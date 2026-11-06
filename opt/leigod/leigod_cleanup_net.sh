#!/bin/sh
# Surgical cleanup of Leigod accelerator iptables chains, policy routes, ipsets, and tun interfaces.
# Never flushes host firewall tables (safe alongside UFW, firewalld, and Docker).
set -u

cleanup_iptables_table() {
    table=$1
    command -v iptables >/dev/null 2>&1 || return 0
    iptables -t "$table" -nL GAMEACC >/dev/null 2>&1 || return 0

    for hook in PREROUTING INPUT OUTPUT FORWARD POSTROUTING; do
        while iptables -t "$table" --delete "$hook" -j GAMEACC 2>/dev/null; do :; done
    done
    iptables -t "$table" --flush GAMEACC 2>/dev/null || true
    iptables -t "$table" --delete-chain GAMEACC 2>/dev/null || true
}

for tbl in mangle nat raw filter; do
    cleanup_iptables_table "$tbl"
done

if command -v iptables >/dev/null 2>&1; then
    iptables -t mangle -S OUTPUT 2>/dev/null | grep -E '(--set-xmark 0x10[1-5]|--uid-owner 0 -j RETURN|-d (10\.0\.0\.0/8|172\.16\.0\.0/12|192\.168\.0\.0/16|224\.0\.0\.0/4|255\.255\.255\.255/32).*RETURN|-o lo -j RETURN)' | while read -r line; do
        rule_spec=${line#-A }
        # shellcheck disable=SC2086
        iptables -t mangle --delete $rule_spec 2>/dev/null || true
    done
fi

if command -v ip >/dev/null 2>&1; then
    while ip rule del fwmark 0x99 lookup 99 2>/dev/null; do :; done
    ip route flush table 99 2>/dev/null || true

    for idx in 1 2 3 4 5; do
        while ip rule del fwmark "0x10$idx" lookup "10$idx" 2>/dev/null; do :; done
        ip route flush table "10$idx" 2>/dev/null || true
    done

    for tun_dev in $(ip -o link show type tun 2>/dev/null | awk -F': ' '{print $2}' | cut -d'@' -f1); do
        case "$tun_dev" in
            tun_*|acc*|leigod*)
                ip link delete "$tun_dev" 2>/dev/null || true
                ;;
        esac
    done
fi

if command -v ipset >/dev/null 2>&1; then
    for set_name in $(ipset list -n 2>/dev/null | grep -E '^(target_|direct_|proxy_)'); do
        ipset flush "$set_name" 2>/dev/null || true
        ipset destroy "$set_name" 2>/dev/null || true
    done
fi

exit 0
