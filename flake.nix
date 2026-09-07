{
  description = "giveaway.quest - give away digital codes to real Fediverse people";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  };

  outputs =
    { self, nixpkgs }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
        "x86_64-darwin"
        "aarch64-darwin"
      ];
      forAll = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});

      daisyuiVersion = "5.7.28";
      daisyui =
        pkgs:
        pkgs.fetchurl {
          url = "https://github.com/saadeghi/daisyui/releases/download/v${daisyuiVersion}/daisyui.mjs";
          hash = "sha256-9QhShLtWIqm6/NqRCKFJsGO8pzyrXu3l8PF6UAbgKEc=";
        };
      daisyuiTheme =
        pkgs:
        pkgs.fetchurl {
          url = "https://github.com/saadeghi/daisyui/releases/download/v${daisyuiVersion}/daisyui-theme.mjs";
          hash = "sha256-yRX66cxTwagM6CUc0e3qY1xbPGQMCv3FJpNA7APGh/M=";
        };
    in
    {
      devShells = forAll (
        pkgs:
        let
          python = pkgs.python313;

          # Tailwind v4 standalone CLI + daisyUI as a local plugin file, so no node_modules.
          build-css = pkgs.writeShellScriptBin "build-css" ''
            set -euo pipefail
            cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
            mkdir -p assets/vendor giveaway_quest/static
            ln -sf ${daisyui pkgs} assets/vendor/daisyui.mjs
            ln -sf ${daisyuiTheme pkgs} assets/vendor/daisyui-theme.mjs
            exec ${pkgs.tailwindcss_4}/bin/tailwindcss -i assets/app.css -o giveaway_quest/static/app.css "$@"
          '';

          dev = pkgs.writeShellScriptBin "dev" ''
            set -euo pipefail
            build-css --minify
            exec uv run gq serve --reload --debug "$@"
          '';
        in
        {
          default = pkgs.mkShell {
            packages = [
              python
              pkgs.uv
              pkgs.tailwindcss_4
              pkgs.sqlite
              pkgs.ruff
              build-css
              pkgs.ty
              dev
            ];

            env = {
              # Always use the nix-provided interpreter; never let uv download its own.
              UV_PYTHON = "${python}/bin/python";
              UV_PYTHON_DOWNLOADS = "never";
            };

            shellHook = ''
              export GQ_DATA_DIR="''${GQ_DATA_DIR:-$PWD/data}"
              if [ -f .env ]; then set -a; . ./.env; set +a; fi
              uv sync --quiet
              echo "giveaway.quest dev shell"
              echo "  dev                 build css + run server with reload"
              echo "  build-css --watch   rebuild tailwind on template changes"
              echo "  uv run gq --help    admin CLI"
            '';
          };
        }
      );
    };
}
