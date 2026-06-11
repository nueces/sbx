FROM sbx-base AS sbx-final

USER agent
WORKDIR /home/agent

ENV PATH="/home/agent/.local/bin:/home/agent/.nodejs/bin:${PATH}"


# uv, python tooling
RUN mkdir ~/.local/bin -p && \
    curl -LsSf https://astral.sh/uv/install.sh | sh && \
    uv tool install specify-cli --from git+https://github.com/github/spec-kit.git

# npm packages
RUN npm config set prefix ~/.nodejs && \
    npm install --prefix ~/.nodejs --ignore-scripts @earendil-works/pi-coding-agent && \
    ln -s ~/.nodejs/node_modules/.bin/pi ~/.local/bin/pi
