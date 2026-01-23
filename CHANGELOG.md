# Changelog

All notable changes to this project will be documented in this file.


## [2.0.0](https://github.com/qtsone/proxmox-ntfy/compare/v1.1.0...v2.0.0) (2025-11-04)


### Features

* **proxmox:** implement API Token authentication support
* **proxmox:** add ability to specify port and thus compatability with setups behind a reverse proxy
* **permissions:** implement automatic permission checking for API tokens
* **permissions:** support per-node permission checking - only monitor nodes with Sys.Audit permission
* **permissions:** graceful handling of partial permissions - monitor accessible nodes and exclude others


### Minor Improvements

* **proxmox:** new environment variable for SSL verification
* **environment variables:** remove defaults. Add hints in logging for missing environment variables. Exit when mandatory environment variables are missing
* **documentation:** provide documented ´example.env´
* **logging:** better connection error messages and diagnostics
* **logging:** set default log level to INFO (from DEBUG)
* **docker:**  removed version: '3' from compose files
* **logging:** add detailed permission checking logs showing which nodes are monitored/excluded
* **logging:** improve error messages for permission issues with helpful troubleshooting steps
* **permissions:** use API to verify token permissions
* **error handling:** add clean exit handling for permission and connection errors

## [1.1.1](https://github.com/qtsone/proxmox-ntfy/compare/v1.1.0...v1.1.1) (2024-07-09)


### Bug Fixes

* **ci:** use pr-checks environment ([9256d78](https://github.com/qtsone/proxmox-ntfy/commit/9256d78f002c4dc02b482b10b8b26ede1d85d980))
* **src:** remove Cache class ([47ce286](https://github.com/qtsone/proxmox-ntfy/commit/47ce286ba24d482ed34a06d9ef99b13e4c02ab29))

# [1.1.0](https://github.com/qtsone/proxmox-ntfy/compare/v1.0.2...v1.1.0) (2024-04-13)


### Features

* **notifications:** implement logging ([f01bc8f](https://github.com/qtsone/proxmox-ntfy/commit/f01bc8fc2ff59846f1b8f11e8ee3581c80f026e8))

## [1.0.2](https://github.com/qtsone/proxmox-ntfy/compare/v1.0.1...v1.0.2) (2024-04-13)


### Bug Fixes

* **proxmox:** align proxmox creds ([4900466](https://github.com/qtsone/proxmox-ntfy/commit/490046617421ceae02a6c9174dc941eb7b1893c0))

## [1.0.1](https://github.com/qtsone/proxmox-ntfy/compare/v1.0.0...v1.0.1) (2024-04-13)


### Bug Fixes

* **release:** use deploy key ([7903d03](https://github.com/qtsone/proxmox-ntfy/commit/7903d03e76729adb27a075100a42930a9437ccb1))

# 1.0.0 (2024-04-13)


### Bug Fixes

* **docker:** update dockerfile-path ([06dca74](https://github.com/qtsone/proxmox-ntfy/commit/06dca748f70c86a83e7cc5eccbee78e5a6e4fb22))
* **docker:** use main branch ([b791f42](https://github.com/qtsone/proxmox-ntfy/commit/b791f42f1b67750f5746dcac269bbbea07330ec3))
* **github:** add repository configuration ([74ad254](https://github.com/qtsone/proxmox-ntfy/commit/74ad2548f58d3b122e6180f24cf1a9e05b6b669d))
* **github:** configuration updates ([80acfc2](https://github.com/qtsone/proxmox-ntfy/commit/80acfc2f139ee0681a6883cc39774817a52ac145))
* **release:** update configuration ([717bce4](https://github.com/qtsone/proxmox-ntfy/commit/717bce414b4d29addf9795cca8efc13718a89d90))


### Features

* **app:** add code ([5b8771f](https://github.com/qtsone/proxmox-ntfy/commit/5b8771ff0d1e550d0ca46206b620fc301dac0267))
* **github:** setup workflows ([e1ab761](https://github.com/qtsone/proxmox-ntfy/commit/e1ab761fe2dd39d9897de15f6c50385b6cd3b1bd))
