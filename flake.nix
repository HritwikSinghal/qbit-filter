{
  description = "qbit-filter: local web UI grouping qBittorrent torrents";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = nixpkgs.legacyPackages.${system};

        # `nix run .#qbit` -- uv-driven host run against the live source tree.
        # Fast iteration loop; uses uv from nixpkgs so the toolchain is pinned.
        qbit = pkgs.writeShellApplication {
          name = "qbit";
          runtimeInputs = [ pkgs.uv ];
          text = ''
            if [ ! -f pyproject.toml ]; then
              echo "qbit: pyproject.toml not found in $PWD -- run from project root" >&2
              exit 1
            fi
            uv sync
            exec uv run python -m qbit_filter "$@"
          '';
        };

        # `nix run .#qbit-dev` -- uvicorn --reload over the live source tree.
        # Watches src/ for *.py *.html *.css *.js, sets DEV_MODE=1 so the
        # browser livereload script polls /dev/version and refreshes on
        # worker restart.
        qbit-dev = pkgs.writeShellApplication {
          name = "qbit-dev";
          runtimeInputs = [ pkgs.uv ];
          text = ''
            if [ ! -f pyproject.toml ]; then
              echo "qbit-dev: pyproject.toml not found in $PWD -- run from project root" >&2
              exit 1
            fi
            uv sync
            export DEV_MODE=1
            exec uv run uvicorn qbit_filter.app:create_app \
              --factory \
              --reload \
              --reload-dir src \
              --reload-include '*.py' \
              --reload-include '*.html' \
              --reload-include '*.css' \
              --reload-include '*.js' \
              --host "''${LISTEN_HOST:-127.0.0.1}" \
              --port "''${LISTEN_PORT:-8765}" \
              "$@"
          '';
        };

        # `nix run .#qbit-docker` -- build + run via docker compose
        # (host 8080 -> container 8765). Uses host docker, not nix-provided.
        qbit-docker = pkgs.writeShellApplication {
          name = "qbit-docker";
          runtimeInputs = [ ];
          text = ''
            if [ ! -f docker-compose.yml ]; then
              echo "qbit-docker: docker-compose.yml not found in $PWD -- run from project root" >&2
              exit 1
            fi
            if ! command -v docker >/dev/null 2>&1; then
              echo "qbit-docker: docker not installed on host" >&2
              exit 1
            fi
            exec docker compose up --build "$@"
          '';
        };
      in
      {
        packages = {
          inherit qbit qbit-dev qbit-docker;
          default = qbit;
        };

        apps = {
          qbit = {
            type = "app";
            program = "${qbit}/bin/qbit";
          };
          qbit-dev = {
            type = "app";
            program = "${qbit-dev}/bin/qbit-dev";
          };
          qbit-docker = {
            type = "app";
            program = "${qbit-docker}/bin/qbit-docker";
          };
          default = self.apps.${system}.qbit;
        };
      });
}
