#!/usr/bin/env bash

param=$1

export DO_TOKEN=$OCF_RESKEY_do_token
IP=$OCF_RESKEY_floating_ip
ID=$(curl -s http://169.254.169.254/metadata/v1/id)
HAS_FLOATING_IP=`curl -s http://169.254.169.254/metadata/v1/floating_ip/ipv4/active`

meta_data() {
  cat <<END
<?xml version="1.0"?>
<!DOCTYPE resource-agent SYSTEM "ra-api-1.dtd">
<resource-agent name="foobar" version="0.1">
  <version>0.1</version>
  <longdesc lang="en">
floatip ocf resource agent for claiming a specified Floating IP via the DigitalOcean API</longdesc>
  <shortdesc lang="en">Assign Floating IP via DigitalOcean API</shortdesc>
<parameters>
<parameter name="do_token" unique="0" required="1">
<longdesc lang="en">
DigitalOcean API Token with Read/Write Permissions
</longdesc>
<shortdesc lang="en">DigitalOcean API Token</shortdesc>
</parameter>
<parameter name="floating_ip" unique="0" required="1">
<longdesc lang="en">
Floating IP to reassign
</longdesc>
<shortdesc lang="en">Floating IP</shortdesc>
</parameter>
</parameters>
  <actions>
    <action name="start"        timeout="20" />
    <action name="stop"         timeout="20" />
    <action name="monitor"      timeout="20"
                                interval="10" depth="0" />
    <action name="meta-data"    timeout="5" />
  </actions>
</resource-agent>
END
}

if [ "start" == "$param" ] ; then
  bash /usr/local/bin/assign-ip $IP $ID
  exit 0
elif [ "stop" == "$param" ] ; then
  exit 0;
elif [ "status" == "$param" ] ; then
  if $HAS_FLOATING_IP ; then
    echo "Has Floating IP"
    exit 0
  else
    echo "Does Not Have Floating IP"
    exit 1
  fi
elif [ "monitor" == "$param" ] ; then
  if $HAS_FLOATING_IP ; then
    exit 0
  else
    exit 7
  fi
elif [ "meta-data" == "$param" ] ; then
  meta_data
  exit 0
else
  echo "no such command $param"
  exit 1;
fi
