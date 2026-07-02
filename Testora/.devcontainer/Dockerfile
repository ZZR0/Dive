FROM mcr.microsoft.com/devcontainers/python:1-3.11-bookworm

# -----------------------------------------------------------------------------
#  Fix yarn signature issue, open bug with microsoft images
#   https://github.com/devcontainers/images/issues/1752
# -----------------------------------------------------------------------------
RUN curl -fsSL https://dl.yarnpkg.com/debian/pubkey.gpg \
  | sudo gpg \
        --batch \
        --yes \
        --dearmor \
        -o /usr/share/keyrings/yarn-archive-keyring.gpg