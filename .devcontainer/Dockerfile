FROM mcr.microsoft.com/devcontainers/python:3.14-bookworm

USER root
# The Python devcontainer image includes zsh and Oh My Zsh via common-utils.
RUN test -d /home/vscode/.oh-my-zsh \
    && chsh -s /usr/bin/zsh vscode \
    && python3 -m venv /opt/venv \
    && chown -R vscode:vscode /opt/venv

# Docker exec / TRAMP must see the same Python tools as interactive terminals.
ENV VIRTUAL_ENV=/opt/venv
ENV PATH="/opt/venv/bin:${PATH}"
COPY profile.sh /etc/profile.d/99-harmonist.sh
COPY --chown=vscode:vscode zshrc /home/vscode/.zshrc
USER vscode
WORKDIR /workspaces/harmonist
CMD ["sleep", "infinity"]
