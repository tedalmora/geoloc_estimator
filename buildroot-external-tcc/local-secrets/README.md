# Credenciais locais do hotspot e do SSH

Os arquivos reais desta pasta sao locais e ignorados individualmente pelo Git:

- `hostapd.conf`: configuracao completa do hotspot, incluindo a senha WPA2;
- `root_password_hash`: hash SHA-512 da senha de root para `/etc/shadow`.

Crie os arquivos no Ubuntu, antes do build. Proteja a pasta e os arquivos:

```sh
cd buildroot-external-tcc
mkdir -p local-secrets
chmod 700 local-secrets
cp local-secrets/hostapd.conf.example local-secrets/hostapd.conf
chmod 600 local-secrets/hostapd.conf
vi local-secrets/hostapd.conf
openssl passwd -6 > local-secrets/root_password_hash
chmod 600 local-secrets/root_password_hash
```

Edite somente `wpa_passphrase` em `hostapd.conf`, usando uma senha WPA2 de 8 a
63 caracteres. `openssl passwd -6` solicita a senha root e grava somente o hash
no arquivo indicado; nao passe a senha como argumento nem a registre em logs.
O exemplo `root_password_hash.example` e apenas um marcador e nao e um hash
utilizavel.

O post-build valida ambos os arquivos e interrompe o build se estiverem
ausentes ou invalidos. Ele copia `hostapd.conf` para `/etc/tcc/hostapd.conf`
com modo 0600 e substitui somente o campo de senha de root em
`TARGET_DIR/etc/shadow`. O hash e a senha nao sao impressos. Os arquivos `.example`
e este README podem ser versionados; nunca adicione os arquivos reais ao Git.
