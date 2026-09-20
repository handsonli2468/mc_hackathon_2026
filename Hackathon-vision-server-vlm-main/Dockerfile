FROM ubuntu:24.04 AS locate
ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends git cmake ninja-build build-essential libvulkan-dev glslc spirv-headers glslang-tools ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /src
RUN git clone --recursive --branch v0.1.0 https://github.com/mudler/locate-anything.cpp.git locate
# LA_PATCH=1: LA_SYSTEM_PROMPT and per-box scores (TODO.md section 3); 0 builds upstream for comparison.
ARG LA_PATCH=1
COPY deploy/patches /patches
RUN if [ "$LA_PATCH" = 1 ]; then git -C locate apply /patches/locate-anything-v0.1.0.patch; fi
RUN cmake -S locate -B locate/build -G Ninja -DCMAKE_BUILD_TYPE=Release -DLA_BUILD_TESTS=OFF -DLA_BUILD_CLI=ON -DLA_SHARED=ON -DLA_GGML_VULKAN=ON && cmake --build locate/build -j 4
RUN mkdir /artifacts && find locate/build -name '*.so*' -type f -exec cp {} /artifacts/ \; && git -C locate rev-parse HEAD > /artifacts/locate-revision.txt

FROM ubuntu:24.04
ARG DEBIAN_FRONTEND=noninteractive
ARG TORCH_INDEX=https://download.pytorch.org/whl/cpu
ARG TORCH_SPEC=torch==2.11.0
ARG TORCHVISION_SPEC=torchvision==0.26.0
ARG SAM_REV=2b90b9f5ceec907a1c18123530e92e794ad901a4
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-venv python3-dev build-essential git ca-certificates libgl1 libglib2.0-0 libgomp1 libusb-1.0-0 libvulkan1 mesa-vulkan-drivers vulkan-tools libnuma1 && rm -rf /var/lib/apt/lists/*
RUN python3 -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH LD_LIBRARY_PATH=/opt/locate PYTHONUNBUFFERED=1 SAM2_BUILD_CUDA=0
RUN pip install --no-cache-dir --upgrade pip setuptools wheel
RUN pip install --no-cache-dir "${TORCH_SPEC}" "${TORCHVISION_SPEC}" --index-url "${TORCH_INDEX}"
# Freeze the selected framework so later installs cannot replace the AMD build.
RUN python -c "import importlib.metadata as m; print('\n'.join(n+'=='+m.version(n) for n in ['torch','torchvision']))" > /opt/framework-constraints.txt
ENV PIP_CONSTRAINT=/opt/framework-constraints.txt PYTHONFAULTHANDLER=1
RUN git clone https://github.com/facebookresearch/sam2.git /opt/sam2 && git -C /opt/sam2 checkout ${SAM_REV} && pip install --no-cache-dir --no-build-isolation /opt/sam2
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY --from=locate /artifacts /opt/locate
WORKDIR /app
COPY vlm_server /app/vlm_server
COPY scripts /app/scripts
COPY tests /app/tests
COPY tools /app/tools
RUN pip check && pip freeze > /opt/python-packages.txt
ENV LOCATE_LIBRARY=/opt/locate/liblocate_anything.so
CMD ["python", "-m", "vlm_server.main"]
