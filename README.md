# Install ComputeStacks

Before proceeding, be sure to review our [architecture overview](https://docs.computestacks.com/admin_guide/getting_started/architecture_overview/), and our [minimum requirements](https://docs.computestacks.com/admin_guide/getting_started/onprem-demo/). We are also more than happy to help you design your ComputeStacks environments. Please [contact us](https://www.computestacks.com/contact) to learn more.

_Note: We do not recommend running this on an existing cluster at this time. We have made efforts to ensure this script is idempotent, however we are not 100% there just yet._

## Prerequisites

1.  You will need a valid license to proceed with the installation. You can purchase, or request a demo license, by visiting: [accounts.computestacks.com/store/computestacks](https://accounts.computestacks.com/store/computestacks)

2.  Ensure your local machine:
    * Has ansible installed and configured, with the following plugins installed:

        ```bash
        ansible-galaxy install -r requirements.yml
        ```

### Inventory File

If you used one of our [terraform setup scripts](https://github.com/ComputeStacks?q=terraform&type=&language=) to create your cluster, then you should already have a skeleton `inventory.yml` file that you can place in this directory. Otherwise, `cp inventory.yml.sample inventory.yml`.

Ensure you fill out all the variables and ensure everything is correct. To see all available settings, check each [role](roles/) and look at their `defaults/main.yml` file.

### DNS

Various parts of this ansible package will require the domains defined in your `inventory.yml` file to be correctly setup. Please configure those domains and ensure the changes have been propagated before proceeding.

Example DNS Settings:

```
a.dev.cmptstks.net. IN A %{floating_ip}
metrics.dev.cmptstks.net. IN A %{PUBLIC IP OF METRICS SERVER}
*.a.dev.cmptstks.net. IN CNAME a.dev.cmptstks.net.
portal.dev.cmptstks.net. IN A %{controller ip address}
cr.dev.cmptstks.net. IN CNAME portal.dev.cmptstks.net.
```

### Requirements for each server

_**Note:** If you used one of our [terraform setup scripts](https://github.com/ComputeStacks?q=terraform&type=&language=), then the following steps would have already been performed for you._

#### Server Hostnames

Ensure all hostnames are configured properly on each node.
Our system expects the hostnames on nodes to be 1 lowercase word (dashes or underscores are ok), no special characters or spaces. They should not be FQDN or have the dot notation.

Examples: node101, node102, node103

```bash
hostname node101 && echo "node101" > /etc/hostname && echo "127.0.0.1 node101" >> /etc/hosts
```

The reason these are critical are two fold:

a) When metrics are stored for containers or nodes, the hostname is added as a label. This is how ComputeStacks is able to filter metrics for the correct container/node.
b) Backup jobs are assigned to the node by their hostname. If there is a mismatch in the controller, then jobs will not run.


#### Distribution Specific Notes

##### Debian 10

Install the following packages

```bash
apt update && apt -y install openssl ca-certificates linux-headers-amd64 python3 python3-pip python3-openssl python3-apt python3-setuptools python3-wheel && pip3 install ansible
```
##### CentOS 7

Please ensure the following has been run on each server.

```bash
yum -y update && yum -y install epel-release kernel-headers && yum -y install ansible
```

Additionally, we require selinux to be enabled on CentOS 7.

<details>
<summary>How to enable selinux</summary>
<ul>
<li> Check if it's enabled and active by running: <code>sestatus</code></li>
<li>
    If <code>SELinux status:</code> is not <code>enabled</code>, then please
    <ul>
        <li>Edit <code>/etc/selinux/config</code> and set <code>SELINUX=enforcing</code></li>
        <li><code>touch /.autorelabel</code></li>
        <li><code>reboot</code></li>
    </ul>
</li>
</ul>
</details>


#### Network MTU Settings

If you're using a setting other than the default `1500`, please add the following to the main `vars:` section of your `inventory.yml` file:

```yaml
container_network_mtu: 1400 # Set the desired MTU for containers to use
```

#### IPv6 for Container Nodes

Due to an ongoing issue with the container network platform we use, IPv6 is currently not supported on our container nodes. We're able to bring ipv6 connectivity by either using a dedicated load balancer on a separate virtual machine, or by configuring the controller to proxy ipv6 traffic.

For the time being, our installer will disable ipv6 directly on the node. However, we recommend also cleaning out the `/etc/sysconfig/network-scripts/ifcfg-*` files to remove any ipv6 specific settings, and setting `IPV6INIT=no`.

We recommend performing this step prior to running this ansible package.

***
## Running

```bash
ansible-playbook -u root -i inventory.yml main.yml --tags "bootstrap"
```

The last step in this script will reboot servers to finalize configuration.


## Post-Installation

After running and allowing the servers to reboot, you can perform some basic validation by running: 

```bash
ansible-playbook -u root -i inventory.yml main.yml --tags "validate"
```

## Adding Nodes
To add nodes to existing Availability Zone, simply add the new nodes to the same inventory file you used to deploy the initial cluster and change `etcd_initial_cluster_state` for each _NEW_ node from `new` to `existing`. Existing nodes _MUST_ remain at `new`.

Please note that you will need to ensure the number of nodes you're deploying will keep the etcd cluster in quorum. As per the etcd documentation, this should be `(n/2)+1` nodes. So 3, 5, 7, or 9 nodes per availability zone.

If you're moving from a single-node to a cluster, you _may_ need to change the `peer url` of the existing standalone etcd server.

<details>
<summary>Set the etcd peer URL</summary>
<p>
Login to the existing node and set the correct peer url.
<br><br>
<em>Note: If your peer url is already configured as defined below, you may skip this process.</em>
<ol>
<li>Find your memberID by running: <code>etcdctl member list | cut -d, -f1</code></li>
<li>
Update the peer url to match the client url, but with port <code>2380</code>
<ul><li><code>etcdctl member update 4370888ee4d9c217 --peer-urls="https://192.168.217.4:2380"</code>. <em>Note the <code>https</code>!</em></li></ul>
</li>
</ol>
</p>
</details>
<br>

**Now you may run the ansible package with:**

```bash
ansible-playbook -u root -i inventory.yml main.yml --tags "addnode"
```

## Add Region / Availability Zone
To add a new region or availability zone, make a copy of the previous `inventory.yml` file and make the following changes:


## FAQ

### How to install ansible

<details>
<summary>Mac OSX</summary>
<p>

[Install Homebrew](https://docs.brew.sh/Installation)

```bash
brew install ansible
```

</p>
</details>

<details>
<summary>Linux</summary>
<p>

[Install pyenv](https://github.com/pyenv/pyenv) for your local (non-root) user account.

You can set the new version with `pyenv global 3.9.1` _(replace `3.9.1` with the version you installed)_

```bash
python -m pip install --user ansible
echo 'export PATH="$PATH:$HOME/.local/bin"' >> ~/.bashrc
```

_Note: Check if you have a `.bashrc` file, it may be `.bash_profile` for your distribution._

This will ensure you have the most recent version of ansible.

</p>
</details>

## Enable Swap Limit

In order to allow swap limitations set on containers, you need to perform the following on each node:

1) Modify the file `/etc/default/grub`
2) Add `cgroup_enable=memory swapaccount=1` to the existing `GRUB_CMDLINE_LINUX_DEFAULT` setting
3) run `update-grub`
4) reboot

_Note: This can add about 1% of overhead._

## Use DNS Validation

We support DNS challenge validation via CloudFlare during the initial provisioning process. 

You will need the following information to enable this:
* API Token with write access to the zone
* Account ID
* Zone ID (Optional)

Both the Account ID and Zone ID can be found on your main domain overview page in CloudFlare.

You can optionally exclude the Zone ID, however you will then need to enable read/write access to _all_ zones for the API Token.

Once you have that information, update the following values in the `inventory.yml` file:

```
acme_challenge_method: "cloudflare"
acme_cf_token: "" # API Token
acme_cf_account: "" # Account ID
acme_cf_zone: "" # Zone ID (optional)
```

## Troubleshooting
### NetworkManager Hostname Error

How to Resolve `set-hostname: current hostname was changed outside NetworkManager: '<hostname>'` in logs:

Edit `/etc/NetworkManager/NetworkManager.conf` and add `hostname-mode=none` to the `[main]` block, and reboot the server.

**Example:**

```
[main]
#plugins=ifcfg-rh,ibft
hostname-mode=none
```
