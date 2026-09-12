# Extends n8n's official image with the system deps this repo's workflows
# shell out to (ffmpeg, poppler-utils, python3+pip) - the stock n8n image
# has none of these, and there's no way to add them to a running container
# persistently without baking a custom image like this one.
FROM docker.n8n.io/n8nio/n8n:latest

USER root
RUN apk add --no-cache ffmpeg poppler-utils python3 py3-pip

# n8n's base image is Alpine; py3-pip's pip refuses global installs without
# --break-system-packages on recent Alpine releases. This container only
# ever runs n8n + this one render pipeline, so a global install is fine.
COPY render/requirements.txt /tmp/render-requirements.txt
RUN pip install --break-system-packages --no-cache-dir -r /tmp/render-requirements.txt

USER node
