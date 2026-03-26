#!/bin/sh
# Generate a self-signed SSL certificate for YoloVest.
# Replace with Let's Encrypt when you have a domain name.
#
# Usage: ./scripts/generate-ssl-cert.sh

set -e

CERT_DIR="./ssl"
mkdir -p "$CERT_DIR"

echo "Generating self-signed SSL certificate..."
openssl req -x509 -nodes -days 365 \
    -newkey rsa:2048 \
    -keyout "$CERT_DIR/privkey.pem" \
    -out "$CERT_DIR/fullchain.pem" \
    -subj "/C=IN/ST=State/L=City/O=YoloVest/CN=localhost"

echo "Certificate generated:"
echo "  $CERT_DIR/fullchain.pem"
echo "  $CERT_DIR/privkey.pem"
echo ""
echo "To use Let's Encrypt instead (requires a domain):"
echo "  1. Point your domain to your EC2 IP"
echo "  2. Run: sudo certbot certonly --standalone -d yourdomain.com"
echo "  3. Copy certs to ./ssl/ or update docker-compose volume paths"
