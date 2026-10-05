docker stop signal-api
docker rm signal-api

# 2. Pull the latest version (incorporating the new signal-cli fixes)
docker pull bbernhard/signal-cli-rest-api:latest

# 3. Spin it back up (and optionally add the JSON-RPC mode to fix group drops)
docker run -d --name signal-api --restart=always \
  --memory="1.5g" \
  -p 8080:8080 \
  -v /home/dave-bekker/google_home/google-nest-telegram-sync/signal-data:/home/.local/share/signal-cli \
  -e 'MODE=json-rpc-native' \
  -e 'JAVA_OPTS=-Xmx1g' \
  -e 'JSON_RPC_TRUST_NEW_IDENTITIES=always' \
  bbernhard/signal-cli-rest-api:latest