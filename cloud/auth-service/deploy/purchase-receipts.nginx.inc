# Include inside the HTTPS server serving the test authentication service.
# The evidence contract permits a JPEG up to 3 MB, base64 encoded in JSON.
location = /v1/assistant/purchase-receipts {
    client_max_body_size 5m;
    proxy_pass http://127.0.0.1:8080;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto https;
    proxy_connect_timeout 5s;
    proxy_read_timeout 120s;
    proxy_send_timeout 120s;
}
