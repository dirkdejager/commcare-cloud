from __future__ import absolute_import, print_function, unicode_literals

import functools
import os
from collections import namedtuple
from datetime import datetime, timedelta

from fabric import operations, utils
from fabric.api import env, local, parallel, roles, run, sudo
from fabric.colors import red
from fabric.context_managers import cd, shell_env
from fabric.contrib import files
from fabric.contrib.project import rsync_project
from fabric.operations import put

import posixpath

from commcare_cloud.environment.exceptions import EnvironmentException

from ..const import (
    DATE_FMT,
    KEEP_UNTIL_PREFIX,
    OFFLINE_STAGING_DIR,
    RELEASE_RECORD,
    ROLES_ALL_SRC,
    ROLES_CONTROL,
    ROLES_DEPLOY,
    ROLES_MANAGE,
    ROLES_STATIC,
)
from .formplayer import clean_formplayer_releases

GitConfig = namedtuple('GitConfig', 'key value')


def update_code(full_cluster=True):
    roles_to_use = _get_roles(full_cluster)

    @roles(roles_to_use)
    @parallel
    def update(git_tag, subdir=None, code_repo=None, deploy_key=None):
        git_env = {}
        if deploy_key:
            git_env["GIT_SSH_COMMAND"] = "ssh -i {} -o IdentitiesOnly=yes".format(
                os.path.join(env.home, ".ssh", deploy_key)
            )
        code_repo = code_repo or env.code_repo
        code_root = env.code_root
        if subdir:
            code_root = os.path.join(code_root, subdir)
        _update_code_from_previous_release(code_repo, subdir, git_env)
        with cd(code_root), shell_env(**git_env):
            sudo('git remote prune origin')
            # this can get into a state where running it once fails
            # but primes it to succeed the next time it runs
            sudo('git fetch origin --tags -q || git fetch origin --tags -q')
            sudo('git checkout {}'.format(git_tag))
            sudo('git reset --hard {}'.format(git_tag))
            sudo('git submodule sync')
            sudo('git submodule update --init --recursive -q')
            # remove all untracked files, including submodules
            sudo("git clean -ffd")
            # remove all .pyc files in the project
            sudo("find . -name '*.pyc' -delete")

    return update


@roles(ROLES_ALL_SRC)
@parallel
def create_offline_dir():
    run('mkdir -p {}'.format(env.offline_code_dir))


@roles(ROLES_CONTROL)
def sync_offline_dir():
    sync_offline_to_control()
    sync_offline_from_control()


def sync_offline_to_control():
    for sync_item in ['bower_components', 'node_modules', 'wheelhouse']:
        rsync_project(
            env.offline_code_dir,
            os.path.join(OFFLINE_STAGING_DIR, 'commcare-hq', sync_item),
            delete=True,
        )
    rsync_project(
        env.offline_code_dir,
        os.path.join(OFFLINE_STAGING_DIR, 'formplayer.jar'),
    )


def sync_offline_from_control():
    for host in _hosts_in_roles(ROLES_ALL_SRC, exclude_roles=ROLES_DEPLOY):
        run("rsync -rvz --exclude 'commcare-hq/*' {} {}".format(
            env.offline_code_dir,
            '{}@{}:{}'.format(env.user, host, env.offline_releases)
        ))


def _hosts_in_roles(roles, exclude_roles=None):
    hosts = set()
    for role, role_hosts in env.roledefs.items():
        if role in roles:
            hosts.update(role_hosts)

    if exclude_roles:
        hosts = hosts - _hosts_in_roles(exclude_roles)
    return hosts


@roles(ROLES_ALL_SRC)
@parallel
def update_code_offline():
    """
    An online release usually clones from the previous release then tops
    off the new updates from the remote github. Since we can't access the remote
    Github, we do this:

        1. Clone the current release to the user's home directory
        2. Update that repo with any changes from the user's local copy of HQ (in offline-staging)
        3. Clone the user's home repo to the release that is being deployed (code_root)
    """
    clone_current_release_to_home_directory()

    git_remote_url = 'ssh://{user}@{host}{code_dir}'.format(
        user=env.user,
        host=env.host,
        code_dir=os.path.join(env.offline_code_dir, 'commcare-hq')
    )

    local('cd {}/commcare-hq && git push {}/.git {}'.format(
        OFFLINE_STAGING_DIR,
        git_remote_url,
        env.deploy_metadata.deploy_ref,
    ))

    # Iterate through each submodule and push master
    local("cd {}/commcare-hq && git submodule foreach 'git push {}/$path/.git --all'".format(
        OFFLINE_STAGING_DIR,
        git_remote_url,
    ))

    clone_home_directory_to_release()
    with cd(env.code_root):
        sudo('git checkout `git rev-parse {}`'.format(env.deploy_metadata.deploy_ref))
        sudo('git reset --hard {}'.format(env.deploy_metadata.deploy_ref))
        sudo('git submodule update --init --recursive')
        # remove all untracked files, including submodules
        sudo("git clean -ffd")
        sudo('git remote set-url origin {}'.format(env.code_repo))
        sudo("find . -name '*.pyc' -delete")


def clone_current_release_to_home_directory():
    offline_hq_root = os.path.join(env.offline_code_dir, 'commcare-hq')
    if not files.exists(offline_hq_root):
        _clone_code_from_local_path(env.code_current, offline_hq_root, run_as_sudo=False)


def clone_home_directory_to_release():
    _clone_code_from_local_path(os.path.join(env.offline_code_dir, 'commcare-hq'), env.code_root, run_as_sudo=True)


@roles(ROLES_ALL_SRC)
@parallel
def update_bower_offline():
    sudo('cp -r {}/bower_components {}'.format(env.offline_code_dir, env.code_root))


@roles(ROLES_ALL_SRC)
@parallel
def update_npm_offline():
    sudo('cp -r {}/node_modules {}'.format(env.offline_code_dir, env.code_root))


def _upload_and_extract(zippath, strip_components=0):
    zipname = os.path.basename(zippath)
    put(zippath, env.offline_code_dir)

    run('tar -xzf {code_dir}/{zipname} -C {code_dir} --strip-components {components}'.format(
        code_dir=env.offline_code_dir,
        zipname=zipname,
        components=strip_components,
    ))


def _update_code_from_previous_release(code_repo, subdir, git_env):
    code_current = env.code_current
    code_root = env.code_root
    if subdir:
        code_current = os.path.join(code_current, subdir)
        code_root = os.path.join(code_root, subdir)

    if files.exists(code_current, use_sudo=True):
        with cd(code_current), shell_env(**git_env):
            sudo('git submodule foreach "git fetch origin"')
        _clone_code_from_local_path(code_current, code_root)
        with cd(code_root):
            sudo('git remote set-url origin {}'.format(code_repo))
    else:
        with shell_env(**git_env):
            sudo('git clone {} {}'.format(code_repo, code_root))


def _get_submodule_list(path):
    if files.exists(path, use_sudo=True):
        with cd(path):
            return sudo("git submodule | awk '{ print $2 }'").split()
    else:
        return []


def _get_local_submodule_urls(path):
    local_submodule_config = []
    for submodule in _get_submodule_list(path):
        local_submodule_config.append(
            GitConfig(
                key='submodule.{submodule}.url'.format(submodule=submodule),
                value='{path}/.git/modules/{submodule}'.format(
                    path=path,
                    submodule=submodule,
                )
            )
        )
    return local_submodule_config


def _get_remote_submodule_urls(path):
    submodule_list = _get_submodule_list(path)
    with cd(path):
        remote_submodule_config = [
            GitConfig(
                key='submodule.{}.url'.format(submodule),
                value=sudo("git config submodule.{}.url".format(submodule))
            )
            for submodule in submodule_list]
    return remote_submodule_config


def _clone_code_from_local_path(from_path, to_path, run_as_sudo=True):
    cmd_fn = sudo if run_as_sudo else run
    git_local_submodule_config = [
        'git config {} {}'.format(submodule_config.key, submodule_config.value)
        for submodule_config in _get_local_submodule_urls(from_path)
    ]
    git_remote_submodule_config = [
        'git config {} {}'.format(submodule_config.key, submodule_config.value)
        for submodule_config in _get_remote_submodule_urls(from_path)
    ]

    with cd(from_path):
        cmd_fn('git clone {}/.git {}'.format(
            from_path,
            to_path
        ))

    with cd(to_path):
        cmd_fn('git config receive.denyCurrentBranch updateInstead')
        if git_local_submodule_config:
            cmd_fn(' && '.join(git_local_submodule_config))
        cmd_fn('git submodule update --init --recursive')
        if git_remote_submodule_config:
            cmd_fn(' && '.join(git_remote_submodule_config))


def _clone_virtual_env(virtualenv_current, virtualenv_root):
    print('Cloning virtual env')
    # There's a bug in virtualenv-clone that doesn't allow us to clone envs from symlinks
    current_virtualenv = sudo('readlink -f {}'.format(virtualenv_current))
    sudo("virtualenv-clone {} {}".format(current_virtualenv, virtualenv_root))
    # There was a bug for a while that made new machines set up with commcare-cloud
    # reference current in their virtualenvs instead of their absolute path
    # this line automatically detects and fixes that.
    # It's a noop in steady-state but essentially for fixing the issue.
    sudo('sed -i -e "s~{virtualenv_current}~{virtualenv_root}~g" $(find {virtualenv_root}/bin/ -type f)'
         .format(virtualenv_current=virtualenv_current, virtualenv_root=virtualenv_root))


@roles(ROLES_ALL_SRC)
@parallel
def clone_virtualenv():
    _clone_virtual_env(env.py3_virtualenv_current, env.py3_virtualenv_root)


def update_virtualenv(full_cluster=True):
    """
    update external dependencies on remote host

    assumes you've done a code update

    """
    roles_to_use = _get_roles(full_cluster)

    @roles(roles_to_use)
    @parallel
    def update():
        join = functools.partial(posixpath.join, env.code_root)
        exists = functools.partial(files.exists, use_sudo=True)

        # Optimization if we have current setup (i.e. not the first deploy)
        if exists(env.py3_virtualenv_current) and not exists(env.py3_virtualenv_root):
            _clone_virtual_env(env.py3_virtualenv_current, env.py3_virtualenv_root)
        elif not exists(env.py3_virtualenv_current):
            raise EnvironmentException(
                "Virtual environment not found: {}".format(env.py3_virtualenv_current)
            )

        requirements_files = [join("requirements", "prod-requirements.txt")]
        requirements_files.extend(
            join(repo.relative_dest, repo.requirements_path)
            for repo in env.ccc_environment.meta_config.git_repositories
            if repo.requirements_path
        )

        with cd(env.code_root):
            cmd_prefix = 'export HOME=/home/{} && source {}/bin/activate && '.format(
                env.sudo_user, env.py3_virtualenv_root)

            proxy = " --proxy={}".format(env.http_proxy) if env.http_proxy else ""
            sudo("{} pip install --quiet --upgrade --timeout=60{} pip-tools".format(
                cmd_prefix, proxy))
            sudo("{} pip-sync --quiet --pip-args='--timeout=60{}' {}".format(
                cmd_prefix, proxy, " ".join(requirements_files)))

    return update


def create_code_dir(full_cluster=True):
    roles_to_use = _get_roles(full_cluster)

    @roles(roles_to_use)
    @parallel
    def create():
        sudo('mkdir -p {}'.format(env.code_root))

    return create


@roles(ROLES_DEPLOY)
def kill_stale_celery_workers(delay=0):
    with cd(env.code_current):
        sudo(
            'echo "{}/bin/python manage.py '
            'kill_stale_celery_workers" '
            '| at now + {} minutes'.format(env.py3_virtualenv_current, delay)
        )


@roles(ROLES_DEPLOY)
def record_successful_deploy():
    start_time = datetime.strptime(env.deploy_metadata.timestamp, DATE_FMT)
    delta = datetime.utcnow() - start_time
    with cd(env.code_current):
        env.deploy_metadata.tag_commit()
        sudo((
            '%(virtualenv_current)s/bin/python manage.py '
            'record_deploy_success --user "%(user)s" --environment '
            '"%(environment)s" --url %(url)s --minutes %(minutes)s --mail_admins '
            '--commit %(commit)s'
        ) % {
            'virtualenv_current': env.py3_virtualenv_current,
            'user': env.user,
            'environment': env.deploy_env,
            'url': env.deploy_metadata.diff.url,
            'minutes': str(int(delta.total_seconds() // 60)),
            'commit': env.deploy_metadata.deploy_ref
        })


@roles(ROLES_ALL_SRC)
@parallel
def record_successful_release():
    with cd(env.root):
        files.append(RELEASE_RECORD, str(env.code_root), use_sudo=True)


#TODO make this a nicer task
@roles(ROLES_ALL_SRC)
@parallel
def update_current(release=None):
    """
    Updates the current release to the one specified or to the code_root
    """
    if ((not release and not files.exists(env.code_root, use_sudo=True)) or
            (release and not files.exists(release, use_sudo=True))):
        utils.abort('About to update current to non-existant release')

    sudo('ln -nfs {} {}'.format(release or env.code_root, env.code_current))


@roles(ROLES_ALL_SRC)
@parallel
def mark_last_release_unsuccessful():
    # Removes last line from RELEASE_RECORD file
    with cd(env.root):
        sudo("sed -i '$d' {}".format(RELEASE_RECORD))


def git_gc_current():
    with cd(env.code_current):
        sudo('echo "git gc" | at -t `date -d "5 seconds" +%m%d%H%M.%S`')
        sudo('echo "git submodule foreach \'git gc\'" | at -t `date -d "5 seconds" +%m%d%H%M.%S`')


@roles(ROLES_ALL_SRC)
@parallel
def clean_releases(keep=3):
    releases = sudo('ls {}'.format(env.releases)).split()
    current_release = os.path.basename(sudo('readlink {}'.format(env.code_current)))

    to_remove = []
    valid_releases = 0
    with cd(env.root):
        for index, release in enumerate(reversed(releases)):
            if release == current_release or release == os.path.basename(env.code_root):
                valid_releases += 1
            elif files.contains(RELEASE_RECORD, release, use_sudo=True, shell=True):
                valid_releases += 1
                if valid_releases > keep:
                    to_remove.append(release)
            elif files.exists(os.path.join(env.releases, release, KEEP_UNTIL_PREFIX + '*'), use_sudo=True):
                # This has a KEEP_UNTIL file, so let's not delete until time is up
                with cd(os.path.join(env.releases, release)):
                    filepath = sudo('find . -name {}*'.format(KEEP_UNTIL_PREFIX))
                filename = os.path.basename(filepath)
                _, date_to_delete_string = filename.split(KEEP_UNTIL_PREFIX)
                date_to_delete = datetime.strptime(date_to_delete_string, DATE_FMT)
                if date_to_delete < datetime.utcnow():
                    to_remove.append(release)
            else:
                # cleans all releases that were not successful deploys
                to_remove.append(release)

    if len(to_remove) == len(releases):
        print(red('Aborting clean_releases, about to remove every release'))
        return

    if os.path.basename(env.code_root) in to_remove:
        print(red('Aborting clean_releases, about to remove current release'))
        return

    if valid_releases < keep:
        print(red('\n\nAborting clean_releases, {}/{} valid '
                  'releases were found\n\n'.format(valid_releases, keep)))
        return

    for release in to_remove:
        sudo('rm -rf {}/{}'.format(env.releases, release))

    clean_formplayer_releases()

    # as part of the clean up step, run gc in the 'current' directory
    git_gc_current()


def copy_localsettings(full_cluster=True):
    roles_to_use = _get_roles(full_cluster)

    @parallel
    @roles(roles_to_use)
    def copy():
        sudo('cp {}/localsettings.py {}/localsettings.py'.format(env.code_current, env.code_root))

    return copy


@parallel
@roles(ROLES_ALL_SRC)
def copy_components():
    if files.exists('{}/bower_components'.format(env.code_current), use_sudo=True):
        sudo('cp -r {}/bower_components {}/bower_components'.format(env.code_current, env.code_root))
    else:
        sudo('mkdir {}/bower_components'.format(env.code_root))


@parallel
@roles(ROLES_ALL_SRC)
def copy_node_modules():
    if files.exists('{}/node_modules'.format(env.code_current), use_sudo=True):
        sudo('cp -r {}/node_modules {}/node_modules'.format(env.code_current, env.code_root))
    else:
        sudo('mkdir {}/node_modules'.format(env.code_root))


@parallel
@roles(ROLES_STATIC)
def copy_compressed_js_staticfiles():
    if files.exists('{}/staticfiles/CACHE/js'.format(env.code_current), use_sudo=True):
        sudo('mkdir -p {}/staticfiles/CACHE'.format(env.code_root))
        sudo('cp -r {}/staticfiles/CACHE/js {}/staticfiles/CACHE/js'.format(env.code_current, env.code_root))


@roles(ROLES_ALL_SRC)
@parallel
def get_previous_release():
    # Gets second to last line in RELEASES.txt
    with cd(env.root):
        return sudo('tail -2 {} | head -n 1'.format(RELEASE_RECORD))


@roles(ROLES_ALL_SRC)
@parallel
def get_number_of_releases():
    with cd(env.root):
        return int(sudo("wc -l {} | awk '{{ print $1 }}'".format(RELEASE_RECORD)))


@roles(ROLES_ALL_SRC)
@parallel
def ensure_release_exists(release):
    return files.exists(release, use_sudo=True)


def mark_keep_until(full_cluster=True):
    roles_to_use = _get_roles(full_cluster)

    @roles(roles_to_use)
    @parallel
    def mark(keep_days):
        until_date = (datetime.utcnow() + timedelta(days=keep_days)).strftime(DATE_FMT)
        with cd(env.code_root):
            sudo('touch {}{}'.format(KEEP_UNTIL_PREFIX, until_date))

    return mark


@roles(ROLES_ALL_SRC)
@parallel
def apply_patch(filepath):
    destination = '/home/{}/{}.patch'.format(env.user, env.deploy_metadata.timestamp)
    operations.put(
        filepath,
        destination,
    )

    current_dir = sudo('readlink -f {}'.format(env.code_current))
    sudo('git apply --unsafe-paths {} --directory={}'.format(destination, current_dir))


@roles(ROLES_ALL_SRC)
@parallel
def reverse_patch(filepath):
    destination = '/home/{}/{}.patch'.format(env.user, env.deploy_metadata.timestamp)
    operations.put(
        filepath,
        destination,
    )

    current_dir = sudo('readlink -f {}'.format(env.code_current))
    sudo('git apply -R --unsafe-paths {} --directory={}'.format(destination, current_dir))


def _get_roles(full_cluster):
    return ROLES_ALL_SRC if full_cluster else ROLES_MANAGE
