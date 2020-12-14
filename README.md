# Install ComputeStacks

## Prerequisites

Ensure your local machine:
  * Has ansible installed and configured, with the following plugins installed:

      ```bash
      ansible-galaxy collection install community.general
      ansible-galaxy collection install ansible.posix
      ```

  * You've connected at least once to each machine over ssh, or you've configured ansible to automatically accept SSH host keys during the initial connection. (see below)
  * You have a `gcloud-auth.json` file in the root directory of this package.

## Requirements for each server

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

## Running


**Tip:** Add the following to `~/.ansible.cfg` to auto-accept new host keys (first time only, will not affect host key checking on subsequent connections):

```
[ssh_connection]
ssh_args = -o StrictHostKeyChecking=accept-new
```

***

```bash
ansible-playbook -u root -i inventory.yml main.yml
```
