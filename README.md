# Install ComputeStacks

Before proceeding, be sure to review our [architecture overview](https://docs.computestacks.com/admin_guide/getting_started/architecture_overview/), and our [minimum requirements](https://docs.computestacks.com/admin_guide/getting_started/onprem-demo/). We are also more than happy to help you design your ComputeStacks environments. Please [contact us](https://www.computestacks.com/contact) to learn more.

_Note: We do not recommend running this on an existing cluster at this time. We have made efforts to ensure this script is idempotent, however we are not 100% there just yet._

## Prerequisites

1.  You will need a valid license to proceed with the installation. You can purchase, or request a demo license, by visiting: [accounts.computestacks.com/store/computestacks](https://accounts.computestacks.com/store/computestacks)

2.  Ensure your local machine:
    * Has ansible installed and configured, with the following plugins installed:

        ```bash
        ansible-galaxy collection install community.general
        ansible-galaxy collection install ansible.posix
        ```

    * You've connected at least once to each machine over ssh, or you've configured ansible to automatically accept SSH host keys during the initial connection. (see below)

### Inventory File

If you used one of our [terraform setup scripts](https://github.com/ComputeStacks?q=terraform&type=&language=) to create your cluster, then you should already have a skeleton `inventory.yml` file that you can place in this directory. Otherwise, `cp inventory.yml.sample inventory.yml`.

Ensure you fill out all the variables and ensure everything is correct. To see all available settings, check each [role](roles/) and look at their `defaults/main.yml` file.

### DNS

Various parts of this ansible package will require the domains defined in your `inventory.yml` file to be correctly setup. Please configure those domains and ensure the changes have been propagated before proceeding.

Example DNS Settings:

```
a.dev.cmptstks.net. IN A %{floating_ip}
*.a.dev.cmptstks.net. IN CNAME a.dev.cmptstks.net.
portal.dev.cmptstks.net. IN A %{controller ip address}
cr.dev.cmptstks.net. IN CNAME portal.dev.cmptstks.net.
```

### Requirements for each server

_**Note:** If you used one of our [terraform setup scripts](https://github.com/ComputeStacks?q=terraform&type=&language=), then the following steps would have already been performed for you._

1. Ensure all hostnames are configured properly on each node.
   Our system expects the hostnames on nodes to be 1 lowercase word (dashes or underscores are ok), no special characters or spaces. They should not be FQDN or have the dot notation.

   Examples: node101, node1, ch01, containernode01

   ```bash
   hostname node101 && echo "node101" > /etc/hostname && echo "127.0.0.1 node101" >> /etc/hosts
   ```

   The reason these are critical are two fold:

    a) When metrics are stored for containers or nodes, the hostname is added as a label. This is how ComputeStacks is able to filter metrics for the correct container/node.
    b) Backup jobs are assigned to the node by their hostname. If there is a mismatch in the controller, then jobs will not run.

2. Ensure the following are installed prior to running the ansible script.

    ```bash
    yum -y update && yum -y install epel-release kernel-headers && yum -y install ansible
    ```

3. Ensure selinux is enabled and activated

    * Check if it's enabled and active by running: `sestatus`
    * If `SELinux status:` is not `enabled`, then please:
      * Edit `/etc/selinux/config` and set `SELINUX=enforcing`
      * `touch /.autorelabel`
      * `reboot`

***
## Running

```bash
ansible-playbook -u root -i inventory.yml main.yml --tags "bootstrap"
```

The last step in this script will reboot servers to finalize configuration.
