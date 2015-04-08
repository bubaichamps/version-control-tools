#!/usr/bin/env python

# This software may be used and distributed according to the terms of the
# GNU General Public License version 2 or any later version.

# This file is used to run all Mercurial-related tests in this repository.

from __future__ import print_function

import argparse
import imp
import os
import re
import subprocess
import sys

# Mercurial's run-tests.py isn't meant to be loaded as a module. We do it
# anyway.
HERE = os.path.dirname(os.path.abspath(__file__))
RUNTESTS = os.path.join(HERE, 'pylib', 'mercurial-support', 'run-tests.py')
EXTDIR = os.path.join(HERE, 'hgext')

sys.path.insert(0, os.path.join(HERE, 'pylib', 'mercurial-support'))
runtestsmod = imp.load_source('runtests', RUNTESTS)

if __name__ == '__main__':
    if 'VIRTUAL_ENV' not in os.environ:
        activate = os.path.join(HERE, 'venv', 'bin', 'activate_this.py')
        execfile(activate, dict(__file__=activate))
        sys.executable = os.path.join(HERE, 'venv', 'bin', 'python')
        os.environ['VIRTUAL_ENV'] = os.path.join(HERE, 'venv')

    import vcttesting.docker as vctdocker
    from vcttesting.testing import (
        get_docker_state,
        get_extensions,
        get_test_files,
        run_nose_tests,
        produce_coverage_reports,
        prune_docker_orphans,
    )

    # Unbuffer stdout.
    sys.stdout = os.fdopen(sys.stdout.fileno(), 'w', 0)

    parser = argparse.ArgumentParser()
    parser.add_argument('--with-hg')
    parser.add_argument('-C', '--cover', action='store_true')
    parser.add_argument('-j', '--jobs', type=int)
    parser.add_argument('--all-versions', action='store_true',
        help='Test against all marked compatible versions')
    parser.add_argument('--no-hg-tip', action='store_true',
        help='Do not run tests against the @ bookmark of hg')
    parser.add_argument('--no-unit', action='store_true',
        help='Do not run Python unit tests')

    options, extra = parser.parse_known_args(sys.argv)

    # some arguments belong to us only. Don't pass it along to run-tests.py.
    sys.argv = [a for a in sys.argv
        if a not in set(['--all-versions', '--no-hg-tip'])]

    coveragerc = os.path.join(HERE, '.coveragerc')
    coverdir = os.path.join(HERE, 'coverage')
    if not os.path.exists(coverdir):
        os.mkdir(coverdir)

    # run-tests.py's coverage options don't work for us... yet. So, we hack
    # in code coverage manually.
    if options.cover:
        os.environ['COVERAGE_DIR'] = coverdir
        os.environ['CODE_COVERAGE'] = '1'

    # We do our own code coverage. Strip it so run-tests.py doesn't try to do
    # it's own.
    sys.argv = [a for a in sys.argv if a != '--cover']
    verbose = '-v' in sys.argv or '--verbose' in sys.argv

    os.environ['BUGZILLA_USERNAME'] = 'admin@example.com'
    os.environ['BUGZILLA_PASSWORD'] = 'password'

    runner = runtestsmod.TestRunner()

    orig_args = list(sys.argv)

    if not options.with_hg:
        hg = os.path.join(os.path.dirname(sys.executable), 'hg')
        sys.argv.extend(['--with-hg', hg])

    sys.argv.extend(['--xunit',
        os.path.join(HERE, 'coverage', 'results.xml')])

    extensions = get_extensions(EXTDIR)

    test_files = get_test_files(extensions)
    extension_tests = test_files['extension']
    unit_tests = test_files['unit']
    hook_tests = test_files['hook']

    possible_tests = [os.path.normpath(os.path.abspath(a))
                      for a in extra[1:] if not a.startswith('-')]
    sys.argv = [a for a in sys.argv
                if os.path.normpath(os.path.abspath(a)) not in possible_tests]
    requested_tests = []
    for t in possible_tests:
        if t in test_files['all']:
            requested_tests.append(t)
            continue

        if os.path.isdir(t):
            t = os.path.normpath(t)
            for test in test_files['all']:
                common = os.path.commonprefix([t, test])
                common = os.path.normpath(common)
                if common == t and test not in requested_tests:
                    requested_tests.append(test)

            continue

    run_hg_tests = []
    run_unit_tests = []

    # All tests unless we got an argument that is a test.
    if not requested_tests:
        run_hg_tests.extend(extension_tests)
        run_hg_tests.extend(hook_tests)
        run_unit_tests.extend(unit_tests)
    else:
        for t in requested_tests:
            if t in unit_tests:
                run_unit_tests.append(t)
            else:
                run_hg_tests.append(t)

    run_all_tests = run_hg_tests + run_unit_tests

    if not options.jobs and len(run_all_tests) > 1:
        print('WARNING: Not running tests optimally. Specify -j to run tests '
                'in parallel.', file=sys.stderr)

    # We take a snapshot of Docker containers and images before we start tests
    # so we can look for leaks later.
    #
    # This behavior is non-ideal: we should not leak Docker containers and
    # images. Furthermore, if others interact with Docker while we run, bad
    # things will happen. But this is the easiest solution, so hacks win.
    preserve_containers = set()
    preserve_images = set()

    # Enable tests to interact with our Docker controlling script.
    docker_state = os.path.join(HERE, '.docker-state.json')
    docker_url, docker_tls = vctdocker.params_from_env(os.environ)
    docker = vctdocker.Docker(docker_state, docker_url, tls=docker_tls)
    if docker.is_alive():
        os.environ['DOCKER_HOSTNAME'] = docker.docker_hostname

        # We build the base BMO images in the test runner because doing it
        # from tests would be racey. It is easier to do it here instead of
        # complicating code with locks.
        #
        # But only do this if a test we are running utilizes Docker.
        res = get_docker_state(docker, run_all_tests, verbose=verbose)
        os.environ.update(res[0])
        preserve_containers |= res[1]
        preserve_images |= res[2]

    sys.argv.extend(run_hg_tests)

    old_env = os.environ.copy()
    old_defaults = dict(runtestsmod.defaults)

    # This is used by hghave to detect the running Mercurial because run-tests
    # doesn't pass down the version info in the environment of the hghave
    # invocation.
    import mercurial.__version__
    hgversion = mercurial.__version__.version
    os.environ['HGVERSION'] = hgversion

    if options.with_hg:
        clean_env = dict(os.environ)
        clean_env['HGPLAIN'] = '1'
        clean_env['HGRCPATH'] = '/dev/null'
        out = subprocess.check_output('%s --version' % options.with_hg,
                env=clean_env, shell=True)
        out = out.splitlines()[0]
        match = re.search('version ([^\+\)]+)', out)
        if not match:
            print('ERROR: Unable to identify Mercurial version.')
            sys.exit(1)

        hgversion = match.group(1)
        os.environ['HGVERSION'] = hgversion

    res = 0
    if run_hg_tests:
        res = runner.run(sys.argv[1:])
    os.environ.clear()
    os.environ.update(old_env)
    runtestsmod.defaults = dict(old_defaults)

    if options.no_unit:
        run_unit_tests = []

    if run_unit_tests:
        noseres = run_nose_tests(run_unit_tests, options.jobs)
        if noseres:
            res = noseres

    # If we're running the full compatibility run, figure out what versions
    # apply to what and run them.
    if options.all_versions:
        # No need to grab code coverage for legacy versions - it just slows
        # us down.
        # Assertion: we have no tests that only work on legacy Mercurial
        # versions.
        try:
            del os.environ['CODE_COVERAGE']
        except KeyError:
            pass

        mercurials_dir = os.path.normpath(os.path.abspath(os.path.join(
            os.environ['VIRTUAL_ENV'], 'mercurials')))

        # Maps directories/versions to lists of tests to run.
        # We normalize X.Y.Z to X.Y for compatibility because the monthly
        # minor releases of Mercurial shouldn't change behavior. If an
        # extension is marked as compatible with X.Y, we run its tests
        # against all X.Y and X.Y.Z releases seen on disk.
        versions = {}
        for dirver in os.listdir(mercurials_dir):
            if dirver.startswith('.') or dirver == '@':
                continue

            normdirver = '.'.join(dirver.split('.')[0:2])

            tests = versions.setdefault(dirver, set())
            tests |= set(hook_tests)

            for e, m in sorted(extensions.items()):
                for extver in m['testedwith']:
                    normever = '.'.join(extver.split('.')[0:2])

                    if extver == dirver or normever == normdirver:
                        tests |= m['tests']

        def run_hg_tests(version, tests):
            if requested_tests:
                tests = [t for t in tests if t in requested_tests]

            if not tests:
                return

            sys.argv = list(orig_args)
            sys.argv.extend(['--with-hg',
                os.path.join(mercurials_dir, version, 'bin', 'hg')])
            sys.argv.extend(sorted(tests))

            print('Testing with Mercurial %s' % version)
            sys.stdout.flush()
            os.environ['HGVERSION'] = version
            runner = runtestsmod.TestRunner()
            r = runner.run(sys.argv[1:])
            os.environ.clear()
            os.environ.update(old_env)
            runtestsmod.defaults = dict(old_defaults)
            sys.stdout.flush()
            sys.stderr.flush()

        for version, tests in sorted(versions.items()):
            res2 = run_hg_tests(version, tests)
            if res2:
                res = res2

        # Run all tests against @ because we always want to be compatible
        # with the bleeding edge of development.
        if not options.no_hg_tip:
            all_hg_tests = []
            for e, m in extensions.items():
                all_hg_tests.extend(sorted(m['tests']))
            all_hg_tests.extend(hook_tests)
            run_hg_tests('@', all_hg_tests)


    if options.cover:
        produce_coverage_reports(coverdir)

    # Clean up leaked Docker containers and images.
    prune_docker_orphans(docker, preserve_containers, preserve_images)

    sys.exit(res)
