#!/usr/bin/env bash
set -euo pipefail

out_dir=${1:-.local/certs}
if [[ -n ${OPENSSL:-} ]]; then
  openssl_bin=$OPENSSL
elif openssl_bin=$(command -v openssl); then
  :
elif [[ -x /opt/homebrew/bin/openssl ]]; then
  openssl_bin=/opt/homebrew/bin/openssl
else
  printf '%s\n' "OpenSSL was not found; install it or set OPENSSL=/path/to/openssl" >&2
  exit 1
fi

if [[ ! -x $openssl_bin ]]; then
  printf '%s\n' "OPENSSL does not name an executable: $openssl_bin" >&2
  exit 1
fi

mkdir -p "$out_dir"
chmod 700 "$out_dir"

"$openssl_bin" req -x509 -newkey rsa:3072 -days 7 -nodes \
  -subj "/CN=OrbitalLink Dev CA" \
  -keyout "$out_dir/ca.key.pem" -out "$out_dir/ca.cert.pem" >/dev/null 2>&1

"$openssl_bin" req -x509 -newkey rsa:3072 -days 7 -nodes \
  -subj "/CN=OrbitalLink Wrong CA" \
  -keyout "$out_dir/wrong-ca.key.pem" -out "$out_dir/wrong-ca.cert.pem" >/dev/null 2>&1

cat > "$out_dir/ca.conf" <<EOF
[ ca ]
default_ca = orbital_ca

[ orbital_ca ]
database = $out_dir/ca-index.txt
new_certs_dir = $out_dir
serial = $out_dir/ca-serial
default_md = sha256
policy = supplied
unique_subject = no

[ supplied ]
commonName = supplied
organizationName = optional

[ client ]
basicConstraints = critical,CA:FALSE
keyUsage = critical,digitalSignature
extendedKeyUsage = clientAuth
EOF
: > "$out_dir/ca-index.txt"
printf '%s\n' 1000 > "$out_dir/ca-serial"

make_client_request() {
  local name=$1
  local subject=$2
  "$openssl_bin" req -newkey rsa:3072 -nodes -subj "$subject" \
    -keyout "$out_dir/$name.key.pem" -out "$out_dir/$name.csr.pem" >/dev/null 2>&1
}

sign_client() {
  local name=$1
  shift
  "$openssl_bin" ca -batch -notext -config "$out_dir/ca.conf" -extensions client \
    -cert "$out_dir/ca.cert.pem" -keyfile "$out_dir/ca.key.pem" \
    -in "$out_dir/$name.csr.pem" -out "$out_dir/$name.cert.pem" "$@" >/dev/null 2>&1
}

make_client_request client "/CN=orbital-ground"
sign_client client -days 7

make_client_request unauthorized-client "/CN=unauthorized-client"
sign_client unauthorized-client -days 7

make_client_request misleading-subject-client "/CN=unauthorized-client/O=orbital-ground"
sign_client misleading-subject-client -days 7

make_client_request expired-client "/CN=orbital-ground"
sign_client expired-client -startdate 20200101000000Z -enddate 20200102000000Z

make_client_request not-yet-valid-client "/CN=orbital-ground"
sign_client not-yet-valid-client -startdate 20350101000000Z -enddate 20350102000000Z

make_client_request wrong-ca-client "/CN=orbital-ground"
"$openssl_bin" x509 -req -in "$out_dir/wrong-ca-client.csr.pem" \
  -CA "$out_dir/wrong-ca.cert.pem" -CAkey "$out_dir/wrong-ca.key.pem" -CAcreateserial \
  -days 7 -extfile "$out_dir/ca.conf" -extensions client \
  -out "$out_dir/wrong-ca-client.cert.pem" >/dev/null 2>&1

cat > "$out_dir/server.ext" <<'EOF'
subjectAltName=DNS:orbital-ground,IP:127.0.0.1
extendedKeyUsage=serverAuth
EOF
"$openssl_bin" req -newkey rsa:3072 -nodes \
  -subj "/CN=orbital-ground" \
  -keyout "$out_dir/server.key.pem" -out "$out_dir/server.csr.pem" >/dev/null 2>&1
"$openssl_bin" x509 -req -in "$out_dir/server.csr.pem" \
  -CA "$out_dir/ca.cert.pem" -CAkey "$out_dir/ca.key.pem" -CAcreateserial \
  -days 7 -extfile "$out_dir/server.ext" -out "$out_dir/server.cert.pem" >/dev/null 2>&1

chmod 600 "$out_dir"/*.key.pem
rm -f "$out_dir"/*.csr.pem "$out_dir"/*.srl "$out_dir/server.ext" \
  "$out_dir/ca.conf" "$out_dir/ca-index.txt" "$out_dir/ca-index.txt.attr" \
  "$out_dir/ca-index.txt.attr.old" "$out_dir/ca-index.txt.old" \
  "$out_dir/ca-serial" "$out_dir/ca-serial.old" "$out_dir"/[0-9]*.pem
printf '%s\n' "$out_dir"
