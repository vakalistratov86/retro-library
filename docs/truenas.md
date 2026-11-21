# TrueNAS SCALE

Use a SCALE release with Docker-based Custom Apps. The initial deployment was tested on 24.10.2.4; screen labels and supported Compose options may differ in newer releases.

1. Create persistent directories/datasets for the library and application state. Set ownership to the UID/GID that will run the container. Do not grant the app a Docker socket or unrestricted access to all pools.
2. Clone the repository into a dedicated source directory on the NAS and build `retro-library:1.1.0` with `docker build -t retro-library:1.1.0 .`.
3. In **Apps → Discover Apps → Custom App / Install via YAML**, use the example below. Replace all placeholders before installing.

```yaml
services:
  retro-library:
    image: retro-library:1.1.0
    pull_policy: never
    user: "1000:1000"
    restart: unless-stopped
    read_only: true
    cap_drop: [ALL]
    security_opt: [no-new-privileges:true]
    ports:
      - "192.0.2.20:30808:8080" # Replace with the NAS LAN address
    environment:
      APPLE_TV_HOST: "192.0.2.10" # Replace with the Apple TV address
      SYNC_INTERVAL: "60"
      METADATA_ENABLED: "true"
    volumes:
      - /mnt/POOL/retro-library/library:/library:rw
      - /mnt/POOL/retro-library/state:/state:rw
    tmpfs:
      - /tmp:size=67108864,mode=1777
    mem_limit: 512m
    cpus: 1.0
    logging:
      driver: json-file
      options: {max-size: 10m, max-file: "3"}
```

The image contains its own health check. Apple TV being offline is a normal waiting state, not an unhealthy container. Reserve an address for Apple TV in your router if its address changes frequently.

To update, build the new image and edit/redeploy this app only. Retain both data mounts. To roll back, select the previous image and keep the same data directories. No host package installation, privileged container, host networking or recursive permission changes are required.

Official references: [TrueNAS custom apps](https://apps.truenas.com/managing-apps/installing-custom-apps/), [RetroArch playlists and thumbnails](https://docs.libretro.com/guides/roms-playlists-thumbnails/).
