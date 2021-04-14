#!/usr/bin/env python3

# Copyright 2017 SchedMD LLC.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import importlib
import logging
import os
import sys
import shutil
import time
from pathlib import Path
from subprocess import DEVNULL
from functools import reduce, partialmethod
from concurrent.futures import ThreadPoolExecutor

import googleapiclient.discovery
import requests
import yaml


# get util.py from metadata
UTIL_FILE = Path('/tmp/util.py')
try:
    resp = requests.get('http://metadata.google.internal/computeMetadata/v1/instance/attributes/util-script',
                        headers={'Metadata-Flavor': 'Google'})
    resp.raise_for_status()
    UTIL_FILE.write_text(resp.text)
except requests.exceptions.RequestException:
    print("util.py script not found in metadata")
    if not UTIL_FILE.exists():
        print(f"{UTIL_FILE} also does not exist, aborting")
        sys.exit(1)

spec = importlib.util.spec_from_file_location('util', UTIL_FILE)
util = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = util
spec.loader.exec_module(util)
cd = util.cd  # import util.cd into local namespace
NSDict = util.NSDict

Path.mkdirp = partialmethod(Path.mkdir, parents=True, exist_ok=True)

util.config_root_logger(logfile='/tmp/setup.log')
log = logging.getLogger(Path(__file__).name)
sys.excepthook = util.handle_exception

# get setup config from metadata
config_yaml = yaml.safe_load(util.get_metadata('attributes/config'))
cfg = util.Config.new_config(config_yaml)

# load all directories as Paths into a dict-like namespace
dirs = NSDict({n: Path(p) for n, p in dict.items({
    'home': '/home',
    'apps': '/apps',
    'scripts': '/slurm/scripts',
    'prefix': '/usr/local',
    'secdisk': '/mnt/disks/sec',
    'apps_sec': '/mnt/disks/sec/apps',
})})

RESUME_TIMEOUT = 300
SUSPEND_TIMEOUT = 300

CONTROL_MACHINE = cfg.cluster_name + '-controller'



def start_motd():
    """ advise in motd that slurm is currently configuring """
    msg = """
*** Slurm is currently being configured in the background. ***
"""
    Path('/etc/motd').write_text(msg)
# END start_motd()


def end_motd(broadcast=True):
    """ modify motd to signal that setup is complete """
    Path('/etc/motd').write_text("")

    if not broadcast:
        return

    util.run("wall -n '*** Slurm {} setup complete ***'"
             .format(cfg.instance_type))
    if cfg.instance_type != 'controller':
        util.run("""wall -n '
/home on the controller was mounted over the existing /home.
Log back in to ensure your home directory is correct.
'""")
# END start_motd()


def expand_instance_templates():
    """ Expand instance template into instance_defs """

    compute = googleapiclient.discovery.build('compute', 'v1',
                                              cache_discovery=False)
    for pid, instance_def in cfg.instance_defs.items():
        if (instance_def.instance_template and
                (not instance_def.machine_type or not instance_def.gpu_count)):
            template_resp = util.ensure_execute(
                compute.instanceTemplates().get(
                    project=cfg.project,
                    instanceTemplate=instance_def.instance_template))
            if template_resp:
                template_props = template_resp['properties']
                if not instance_def.machine_type:
                    instance_def.machine_type = template_props['machineType']
                if (not instance_def.gpu_count and
                        'guestAccelerators' in template_props):
                    accel_props = template_props['guestAccelerators'][0]
                    instance_def.gpu_count = accel_props['acceleratorCount']
                    instance_def.gpu_type = accel_props['acceleratorType']
# END expand_instance_templates()


def expand_machine_type():
    """ get machine type specs from api """
    machines = {}
    compute = googleapiclient.discovery.build('compute', 'v1',
                                              cache_discovery=False)
    for pid, part in cfg.instance_defs.items():
        machine = {'cpus': 1, 'memory': 1}
        machines[pid] = machine

        if not part.machine_type:
            log.error("No machine type to get configuration from")
            continue

        type_resp = None
        if part.regional_capacity:
            filter = f"(zone={part.region}-*) AND (name={part.machine_type})"
            list_resp = util.ensure_execute(
                compute.machineTypes().aggregatedList(
                    project=cfg.project, filter=filter))

            if 'items' in list_resp:
                zone_types = list_resp['items']
                for k, v in zone_types.items():
                    if part.region in k and 'machineTypes' in v:
                        type_resp = v['machineTypes'][0]
                        break
        else:
            type_resp = util.ensure_execute(
                compute.machineTypes().get(
                    project=cfg.project, zone=part.zone,
                    machineType=part.machine_type))

        if type_resp:
            cpus = type_resp['guestCpus']
            machine['cpus'] = cpus // (1 if part.image_hyperthreads else 2)

            # Because the actual memory on the host will be different than
            # what is configured (e.g. kernel will take it). From
            # experiments, about 16 MB per GB are used (plus about 400 MB
            # buffer for the first couple of GB's. Using 30 MB to be safe.
            gb = type_resp['memoryMb'] // 1024
            machine['memory'] = type_resp['memoryMb'] - (400 + (gb * 30))

    return machines
# END expand_machine_type()


def install_meta_files():
    """ save config.yaml and download all scripts from metadata """
    Path(dirs.scripts).mkdirp()
    cfg.save_config(dirs.scripts/'config.yaml')

    meta_entries = [
        ('util.py', 'util-script'),
        ('setup.py', 'setup-script'),
        ('startup.sh', 'startup-script'),
        ('custom-compute-install', 'custom-compute-install'),
        ('custom-controller-install', 'custom-controller-install'),
    ]

    def install_metafile(filename, metaname):
        text = util.get_metadata('attributes/' + metaname)
        if not text:
            return
        path = dirs.scripts/filename
        path.write_text(text)
        path.chmod(0o755)

    with ThreadPoolExecutor() as exe:
        exe.map(lambda x: install_metafile(*x), meta_entries)

# END install_meta_files()


def prepare_network_mounts(hostname, instance_type):
    """ Prepare separate lists of cluster-internal and external mounts for the
    given host instance, returning (external_mounts, internal_mounts)
    """
    log.info("Set up network storage")

    default_mounts = (
        dirs.home,
        dirs.apps,
        dirs.apps_sec,
    )

    # create dict of mounts, local_mount: mount_info
    CONTROL_NFS = {
        'server_ip': CONTROL_MACHINE,
        'remote_mount': 'none',
        'local_mount': 'none',
        'fs_type': 'nfs',
        'mount_options': 'defaults,hard,intr',
    }
    # seed the non-controller mounts with the default controller mounts
    mounts = {
        path: util.Config(CONTROL_NFS, local_mount=path, remote_mount=path)
        for path in default_mounts
    }

    # convert network_storage list of mounts to dict of mounts,
    #   local_mount as key
    def listtodict(mountlist):
        return {Path(d['local_mount']).resolve(): d for d in mountlist}

    # On non-controller instances, entries in network_storage could overwrite
    # default exports from the controller. Be careful, of course
    mounts.update(listtodict(cfg.network_storage))

    if instance_type == 'compute':
        pid = util.get_pid(hostname)
        mounts.update(listtodict(cfg.instance_defs[pid].network_storage))
    else:
        # login_network_storage is mounted on controller and login instances
        mounts.update(listtodict(cfg.login_network_storage))

    # filter mounts into two dicts, cluster-internal and external mounts, and
    # return both. (external_mounts, internal_mounts)
    def internal_mount(mount):
        return mount[1].server_ip == CONTROL_MACHINE

    def partition(pred, coll):
        """ filter into 2 lists based on pred returning True or False 
            returns ([False], [True])
        """
        return reduce(
            lambda acc, el: acc[pred(el)].append(el) or acc,
            coll, ([], [])
        )

    return tuple(map(dict, partition(internal_mount, mounts.items())))
# END prepare_network_mounts


def setup_network_storage():
    """ prepare network fs mounts and add them to fstab """

    global mounts
    ext_mounts, int_mounts = prepare_network_mounts(cfg.hostname,
                                                    cfg.instance_type)
    mounts = ext_mounts
    if cfg.instance_type != 'controller':
        mounts.update(int_mounts)

    # Determine fstab entries and write them out
    fstab_entries = []
    for local_mount, mount in mounts.items():
        remote_mount = mount.remote_mount
        fs_type = mount.fs_type
        server_ip = mount.server_ip

        # do not mount controller mounts to itself
        if server_ip == CONTROL_MACHINE and cfg.instance_type == 'controller':
            continue

        log.info("Setting up mount ({}) {}{} to {}".format(
            fs_type, server_ip+':' if fs_type != 'gcsfuse' else "",
            remote_mount, local_mount))

        local_mount.mkdirp()

        mount_options = (mount.mount_options.split(',') if mount.mount_options
                         else [])
        if not mount_options or '_netdev' not in mount_options:
            mount_options += ['_netdev']

        if fs_type == 'gcsfuse':
            if 'nonempty' not in mount_options:
                mount_options += ['nonempty']
            fstab_entries.append(
                "{0}   {1}     {2}     {3}     0 0"
                .format(remote_mount, local_mount, fs_type,
                        ','.join(mount_options)))
        else:
            remote_mount = Path(remote_mount).resolve()
            fstab_entries.append(
                "{0}:{1}    {2}     {3}      {4}  0 0"
                .format(server_ip, remote_mount, local_mount,
                        fs_type, ','.join(mount_options)))

    for mount in mounts:
        Path(mount).mkdirp()
    with open('/etc/fstab', 'a') as f:
        f.write('\n')
        for entry in fstab_entries:
            f.write(entry)
            f.write('\n')
# END setup_network_storage()


def mount_fstab():
    """ Wait on each mount, then make sure all fstab is mounted """
    global mounts

    def mount_path(path):
        while not os.path.ismount(path):
            log.info(f"Waiting for {path} to be mounted")
            util.run(f"mount {path}", wait=5)

    with ThreadPoolExecutor() as exe:
        exe.map(mount_path, mounts.keys())

    util.run("mount -a", wait=1)
# END mount_external


def setup_nfs_exports():
    """ nfs export all needed directories """
    # The controller only needs to set up exports for cluster-internal mounts
    # switch the key to remote mount path since that is what needs exporting
    _, con_mounts = prepare_network_mounts(cfg.hostname, cfg.instance_type)
    con_mounts = {m.remote_mount: m for m in con_mounts.values()}
    for pid, _ in cfg.instance_defs.items():
        # get internal mounts for each partition by calling
        # prepare_network_mounts as from a node in each partition
        _, part_mounts = prepare_network_mounts(f'{pid}-n', 'compute')
        part_mounts = {m.remote_mount: m for m in part_mounts.values()}
        con_mounts.update(part_mounts)

    # export path if corresponding selector boolean is True
    exports = []
    for path in con_mounts:
        Path(path).mkdirp()
        util.run(rf"sed -i '\#{path}#d' /etc/exports")
        exports.append(f"{path}  *(rw,no_subtree_check,no_root_squash)")

    exportsd = Path('/etc/exports.d')
    exportsd.mkdirp()
    with (exportsd/'cluster.exports').open('w') as f:
        f.write('\n')
        f.write('\n'.join(exports))
    util.run("exportfs -a")
# END setup_nfs_exports()


def setup_secondary_disks():
    """ Format and mount secondary disk """
    util.run(
        "sudo mkfs.ext4 -m 0 -F -E lazy_itable_init=0,lazy_journal_init=0,discard /dev/sdb")
    Path(dirs.secdisk).mkdirp()
    with open('/etc/fstab', 'a') as f:
        f.write(
            "\n/dev/sdb     {0}     ext4    discard,defaults,nofail     0 2"
            .format(dirs.secdisk))

# END setup_secondary_disks()


def setup_controller():
    """ Run controller setup """
    expand_instance_templates()

    if cfg.controller_secondary_disk:
        setup_secondary_disks()
    setup_network_storage()
    mount_fstab()

    try:
        util.run(str(dirs.scripts/'custom-controller-install'))
    except Exception:
        # Ignore blank files with no shell magic.
        pass

    # Export at the end to signal that everything is up
    util.run("systemctl enable nfs-server")
    util.run("systemctl start nfs-server")

    setup_nfs_exports()

    log.info("Done setting up controller")
    pass


def setup_login():
    """ run login node setup """
    setup_network_storage()
    mount_fstab()

    try:
        util.run(str(dirs.scripts/'custom-compute-install'))
    except Exception:
        # Ignore blank files with no shell magic.
        pass

    log.info("Done setting up login")


def setup_compute():
    """ run compute node setup """
    setup_network_storage()
    mount_fstab()

    pid = util.get_pid(cfg.hostname)
    if cfg.instance_defs[pid].gpu_count:
        retries = n = 50
        while util.run("nvidia-smi").returncode != 0 and n > 0:
            n -= 1
            log.info(f"Nvidia driver not yet loaded, try {retries-n}")
            time.sleep(5)

    try:
        util.run(str(dirs.scripts/'custom-compute-install'))
    except Exception:
        # Ignore blank files with no shell magic.
        pass

    log.info("Done setting up compute")


def main():

    start_motd()
    install_meta_files()

    # call the setup function for the instance type
    setup = dict.get(
        {
            'controller': setup_controller,
            'compute': setup_compute,
            'login': setup_login
        },
        cfg.instance_type,
        lambda: log.fatal(f"Unknown instance type: {cfg.instance_type}")
    )
    setup()

    end_motd()
# END main()


if __name__ == '__main__':
    main()
