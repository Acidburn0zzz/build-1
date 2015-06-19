#!/usr/bin/env python2.7
#+
# Copyright 2015 iXsystems, Inc.
# All rights reserved
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted providing that the following conditions
# are met:
# 1. Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright
#    notice, this list of conditions and the following disclaimer in the
#    documentation and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE AUTHOR ``AS IS'' AND ANY EXPRESS OR
# IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED.  IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY
# DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS
# OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION)
# HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT,
# STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING
# IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.
#
#####################################################################

import os
import subprocess
import imp
import threading
import time
from utils import sh, sh_str, sh_spawn, e, glob, objdir, info, debug, error, load_file, import_function, on_abort


load_file('${BUILD_CONFIG}/tests/bhyve.pyd', os.environ)
installworldlog = objdir('logs/test-installworld')
distributionlog = objdir('logs/test-distribution')
installkernellog = objdir('logs/test-installkernel')
buildkernel = import_function('build-os', 'buildkernel')
installworld = import_function('build-os', 'installworld')
installkernel = import_function('build-os', 'installkernel')
vm_proc = None
termserv_proc = None
vm_wait_thread = None
current_test = None
shutdown = False
tapdev = None


def setup_network():
    global tapdev

    info('Configuring VM networking')
    tapdev = sh_str('ifconfig tap create')
    info('Using tap device {0}', tapdev)
    sh('ifconfig ${tapdev} inet ${HOST_IP} ${NETMASK} up')


def setup_rootfs():
    buildkernel(e('${KERNCONF}'), ['mach'])
    installworld('${OBJDIR}/test-root', installworldlog, distributionlog)
    installkernel('${OBJDIR}/test-root', installkernellog, modules=['mach'])
    info('Installing overlay files')
    sh('rsync -ah ${TESTS_ROOT}/trueos/overlay/ ${OBJDIR}/test-root')
    sh('makefs -M ${IMAGE_SIZE} ${OBJDIR}/test-root.ufs ${OBJDIR}/test-root')


def setup_vm():
    global vm_proc, termserv_proc

    info('Starting up VM')
    sh('bhyveload -m ${RAM_SIZE} -d ${OBJDIR}/test-root.ufs ${VM_NAME}')
    vm_proc = sh_spawn(
        'bhyve -m ${RAM_SIZE} -A -H -P',
        '-s 0:0,hostbridge',
        '-s 1:0,virtio-net,${tapdev}',
        '-s 2:0,ahci-hd,${OBJDIR}/test-root.ufs',
        '-s 31,lpc -l com1,${CONSOLE_MASTER}',
        '${VM_NAME}'
    )

    pid = vm_proc.pid
    logfile = objdir(e('logs/bhyve.${pid}.log'))

    info('Starting telnet server on port {0}', e('${TELNET_PORT}'))
    info('Console log file is {0}', logfile)
    termserv_proc = sh_spawn(
        'python',
        '${BUILD_TOOLS}/terminal-server.py',
        '-l ${logfile}',
        '-c ${CONSOLE_SLAVE}',
        '-p ${TELNET_PORT}'
    )

    on_abort(shutdown_vm)


def wait_vm():
    global vm_wait_thread

    def wait_thread():
        errcode = vm_proc.wait()
        info('VM exited')
        shutdown_vm()

    vm_wait_thread = threading.Thread(target=wait_thread)
    vm_wait_thread.start()


def shutdown_vm():
    termserv_proc.terminate()
    sh('bhyvectl --destroy --vm=${VM_NAME}', nofail=True)
    sh('ifconfig ${tapdev} destroy')


def ssh(command):
    keyfile = e('${TESTS_ROOT}/trueos/overlay/root/.ssh/id_rsa')
    proc = subprocess.Popen(
        [
            'ssh',
            '-o', 'ServerAliveInterval=10',
            '-o', 'StrictHostKeyChecking=no',
            '-o', 'UserKnownHostsFile=/dev/null',
            '-i', keyfile,
            e('root@${VM_IP}'),
            command
        ],
        stderr=subprocess.PIPE,
        stdout=subprocess.PIPE,
        close_fds=True
    )

    debug('Running command on a VM: {0}', command)

    out, err = proc.communicate()

    if proc.returncode != 0:
        debug('Command failed:')
        debug('stdout: {0}', out.strip())
        debug('stderr: {0}', err.strip())

    return proc.returncode, out, err


def main():
    if e('${PLAYGROUND}'):
        info('Type RETURN to kill VM')
        raw_input()
        vm_proc.kill()
        return

    tests_total = len(glob('${TESTS_ROOT}/trueos/*.py'))
    tests_success = []
    tests_failure = []

    for t in sorted(glob('${TESTS_ROOT}/trueos/*.py')):
        testname = os.path.splitext(os.path.basename(t))[0]
        info('Running test {0}', testname)
        mod = imp.load_source(testname, t)
        success, reason = mod.run(ssh)

        # Give VM a while if panic happened
        time.sleep(2)

        if vm_proc.returncode is not None:
            # VM crashed!
            error('Test {0} caused VM crash', testname)

        if success is None:
            error('Test {0} returned aborted test schedule: {1}', testname, reason)
        elif success:
            tests_success.append(testname)
        else:
            info('Failed: {0}', reason)
            tests_failure.append(testname)

    vm_proc.kill()
    info('{0} total tests', tests_total)
    info('{0} successes', len(tests_success))
    info('{0} failures', len(tests_failure))

    if len(tests_failure) > 0:
        info('Failed tests: {0}', ', '.join(tests_failure))


if __name__ == '__main__':
    setup_rootfs()
    setup_network()
    setup_vm()
    wait_vm()
    main()
