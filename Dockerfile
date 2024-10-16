FROM nerfstudio/nerfstudio

WORKDIR /workspace/

USER root

# Install git
RUN apt-get update && apt-get install -y git

# Install pixi
RUN curl -fsSL https://pixi.sh/install.sh | bash

# Add pixi to PATH
ENV PATH="/root/.pixi/bin:${PATH}"

# Clone the repository and run the application
COPY . /workspace/InstantSplat

WORKDIR /workspace/InstantSplat

EXPOSE 7860

RUN pixi install 

CMD ["pixi", "run", "--environment", "default", "app"]