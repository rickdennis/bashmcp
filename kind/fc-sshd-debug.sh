#!/bin/sh
# Boot the rootfs with init launching `sshd -ddd` (foreground debug) so we capture
# the SERVER's exact reason for accepting/refusing the key. Then trigger one SSH
# from the pod with the node-agent key. Run inside a node-agent pod.
set -u
pkill -f firecracker 2>/dev/null || true; sleep 1
ip neigh flush dev fc-br0 2>/dev/null || true
cp --sparse=always /opt/fc-mcp/vm-images/ubuntu-22.04-base.ext4 /tmp/c.ext4
e2fsck -fy /tmp/c.ext4 >/dev/null 2>&1 || true
mkdir -p /mnt/o; mount -o loop /tmp/c.ext4 /mnt/o
cat > /mnt/o/sbin/sshd-debug.sh <<'IEOF'
#!/bin/sh
mount -t proc proc /proc 2>/dev/null
mount -t sysfs sys /sys 2>/dev/null
mount -t devtmpfs dev /dev 2>/dev/null
mkdir -p /run/sshd
ip link set lo up 2>/dev/null
ip link set eth0 up 2>/dev/null
ip addr add 172.16.0.2/24 dev eth0 2>/dev/null
echo SSHD_DEBUG_LISTENING
/usr/sbin/sshd -ddd -e
echo SSHD_EXITED
sleep 5
echo o > /proc/sysrq-trigger 2>/dev/null
sleep 5
IEOF
chmod +x /mnt/o/sbin/sshd-debug.sh
sync; umount /mnt/o
cat > /tmp/fc.json <<'EOF'
{
  "boot-source": {"kernel_image_path": "/opt/fc-mcp/vm-images/vmlinux-5.10", "boot_args": "console=ttyS0 reboot=k panic=1 pci=off ip=172.16.0.2::172.16.0.1:255.255.255.0::eth0:off init=/sbin/sshd-debug.sh"},
  "drives": [{"drive_id":"rootfs","path_on_host":"/tmp/c.ext4","is_root_device":true,"is_read_only":false}],
  "machine-config": {"vcpu_count":1,"mem_size_mib":256},
  "network-interfaces":[{"iface_id":"eth0","host_dev_name":"fc-tap-00000000","guest_mac":"AA:FC:00:00:00:01"}]
}
EOF
ip link set fc-tap-00000000 up 2>/dev/null || true
firecracker --no-api --config-file /tmp/fc.json > /tmp/sshdcon.log 2>&1 &
sleep 15
echo "=== triggering one ssh ==="
ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=8 \
    -o PreferredAuthentications=publickey -o IdentitiesOnly=yes -i /opt/fc-mcp/vm_ssh_key \
    root@172.16.0.2 'echo GUEST_OK' 2>&1 | tail -2
sleep 3
pkill -f firecracker 2>/dev/null || true
echo "===== sshd debug (auth decision) ====="
grep -iE "refused|bad ownership|modes for|matching|authorized_keys|Accepted|Failed|trying|user root|key type|userauth|Postponed|publickey|Connection from|input_userauth" /tmp/sshdcon.log | tail -45
