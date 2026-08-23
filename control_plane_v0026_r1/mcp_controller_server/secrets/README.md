# Runtime secrets

This directory must not contain secret material in a release archive.
Deployment expects externally provisioned files selected through
`MCP_MQTT_CA_SOURCE`, `MCP_MQTT_CERT_SOURCE`, `MCP_MQTT_KEY_SOURCE` and
`MCP_AUTH_SOURCE`. The paths in `.env.example` keep them outside this release.

For the integrated Control Plane bundle, generate the server authentication
file from the protected external credentials document with:

    python3 ../tools/prepare_mcp_auth.py \
      --credentials-file "$HOME/.config/automation-control-plane/credentials.json" \
      --credential-key mcp_controller.local_agent.token \
      --output "$HOME/.config/automation-control-plane/mcp_auth.json"

The standalone generator remains available when a separate client token file
is required:

    python3 scripts/generate_auth.py \
      --auth-file /secure/mcp-controller/mcp_auth.json \
      --token-file /secure/path/mcp_client.token

The command never prints the token. Import the separate token file into the
MCP client secret store and remove any unnecessary copy.
The MQTT private key must have mode 0600 and every secret must remain outside
the immutable package directory and manifest.
