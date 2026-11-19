FROM rust:1.88.0-bookworm AS build

RUN apt-get update && apt-get install -y --no-install-recommends musl-tools \
    && rm -rf /var/lib/apt/lists/* \
    && rustup target add x86_64-unknown-linux-musl
WORKDIR /build
COPY services/rust-read/ ./
RUN cargo build --locked --release --target x86_64-unknown-linux-musl

FROM scratch
COPY --from=build /build/target/x86_64-unknown-linux-musl/release/hyperp-rust-read /rust-read
USER 65532:65532
ENTRYPOINT ["/rust-read"]
