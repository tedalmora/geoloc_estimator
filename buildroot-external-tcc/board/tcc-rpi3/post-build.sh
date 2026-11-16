#!/bin/sh
set -eu

TARGET_DIR=${1:?Buildroot target directory is required}
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
SECRETS_DIR="$SCRIPT_DIR/../../local-secrets"
HOSTAPD_SOURCE="$SECRETS_DIR/hostapd.conf"
ROOT_HASH_SOURCE="$SECRETS_DIR/root_password_hash"
HOSTAPD_EXAMPLE="$SECRETS_DIR/hostapd.conf.example"
RUNTIME_EXAMPLE="$SCRIPT_DIR/rootfs-overlay/etc/tcc/runtime.conf.example"
SHADOW="$TARGET_DIR/etc/shadow"

fail() {
	printf '%s\n' "TCC post-build: $1" >&2
	exit 1
}

[ -f "$HOSTAPD_SOURCE" ] || fail "local hostapd.conf is missing"
[ -f "$ROOT_HASH_SOURCE" ] || fail "local root password hash is missing"
[ -f "$HOSTAPD_EXAMPLE" ] || fail "hostapd example is missing"
[ -f "$RUNTIME_EXAMPLE" ] || fail "runtime.conf example is missing"
[ -f "$SHADOW" ] || fail "target shadow file is missing"

if ! awk -F= '
	/^[[:space:]]*#/ || /^[[:space:]]*$/ { next }
	{
		separator = index($0, "=")
		if (!separator) next
		key = substr($0, 1, separator - 1)
		value = substr($0, separator + 1)
		sub(/\r$/, "", value)
		if (key == "interface") {
			interfaces++
			if (value != "wlan0") invalid = 1
		} else if (key == "ssid") {
			ssids++
			if (value != "TCC-RPI3") invalid = 1
		} else if (key == "wpa") {
			wpa++
			if (value != "2") invalid = 1
		} else if (key == "wpa_key_mgmt") {
			key_mgmt++
			if (value != "WPA-PSK") invalid = 1
		} else if (key == "wpa_passphrase") {
			passphrases++
			if (length(value) < 8 || length(value) > 63 ||
				value == "SUBSTITUIR_LOCALMENTE" ||
				value == "REPLACE_WITH_LOCAL_PASSWORD") invalid = 1
		}
	}
	END {
		if (interfaces != 1 || ssids != 1 || wpa != 1 ||
			key_mgmt != 1 || passphrases != 1 || invalid) exit 1
	}
' "$HOSTAPD_SOURCE"; then
	fail "local hostapd.conf is invalid"
fi

if grep -Eq 'SUBSTITUIR_LOCALMENTE|REPLACE_WITH_LOCAL_PASSWORD' "$HOSTAPD_SOURCE"; then
	fail "local hostapd.conf still contains the placeholder"
fi

if ! awk '
	NR == 1 && /^\$6\$[.\/A-Za-z0-9]{1,16}\$[.\/A-Za-z0-9]{86}$/ { valid = 1 }
	END { if (NR != 1 || !valid) exit 1 }
' "$ROOT_HASH_SOURCE"; then
	fail "local root password hash is invalid"
fi

install -m 0600 "$HOSTAPD_SOURCE" "$TARGET_DIR/etc/tcc/hostapd.conf" ||
	fail "could not install local hostapd.conf"

SHADOW_TMP="$SHADOW.tcc.$$"
umask 077
trap 'rm -f "$SHADOW_TMP"' 0 HUP INT TERM
if ! awk -F: -v hash_file="$ROOT_HASH_SOURCE" '
	BEGIN {
		OFS = ":"
		if ((getline root_hash < hash_file) != 1) exit 1
		close(hash_file)
	}
	$1 == "root" {
		root_entries++
		$2 = root_hash
	}
	{ print }
	END { if (root_entries != 1) exit 1 }
' "$SHADOW" > "$SHADOW_TMP"; then
	fail "target root entry could not be updated"
fi
chmod 0600 "$SHADOW_TMP" || fail "could not protect target shadow file"
mv -f "$SHADOW_TMP" "$SHADOW" || fail "could not replace target shadow file"
trap - 0 HUP INT TERM

if [ ! -e "$TARGET_DIR/etc/tcc/runtime.conf" ]; then
	install -m 0600 "$RUNTIME_EXAMPLE" "$TARGET_DIR/etc/tcc/runtime.conf" ||
		fail "could not install default runtime.conf"
fi

printf '%s\n' "TCC post-build: local access credentials installed."
