# Deployment Guide

This guide provides detailed instructions for setting up and managing the Multi-Vendor ZTP server.

## Prerequisites

- **Docker & Docker Compose**: The server runs entirely as a containerized application.
- **Physical Connectivity**: Your server (laptop or dedicated host) must have a physical interface connected to the management network or directly to the switches.
- **Root/Sudo Access**: Required for Docker network operations (NET_ADMIN capability).

## Installation

1.  **Extract/Clone the Project**:
    Ensure you are in the project root directory containing `start.sh` and `docker-compose.yaml`.

2.  **Ensure Script Permissions**:
    ```bash
    chmod +x start.sh
    ```

## Configuration

### 1. DHCP & dnsmasq
Edit `config/dnsmasq.conf` to match your network environment. Ensure the `dhcp-range` and `interface` settings are correct for your switch-facing connection.

### 2. Inventory Management
The `config/inventory.yaml` file defines how each switch is provisioned.

- **Defaults**: Set global firmware and configuration files here.
- **Switches**: Define specific serial numbers, their platform (eos, eos64, cisco_ios), and their provisioning **priority**.

> [!IMPORTANT]
> **Provisioning Priorities**:
> - Lower numbers (e.g., `1`) provision first.
> - Switches with the same priority number will provision in parallel.
> - Higher priority switches (e.g., `2`) will wait until ALL switches in the lower priority group report successful completion.

### 3. Filesystem Layout
- **`configs/`**: Place `.cfg` files here. These are served to the switches.
- **`firmware/`**: Place Arista `.swi` or Cisco `.bin` images here.
- **`scripts/`**: Contains the core logic and bootstrap scripts.
- **`logs/`**: Container and ZTP event logs are persisted here.

## Running the Server

Use the `./start.sh` script for all operations:

### Initial Build & Start
```bash
./start.sh start
```
This command checks for required directories, builds the Docker image if missing, and launches the container in detached mode.

### Hot-Reloading Inventory
If you modify `inventory.yaml`, you don't need to restart the container:
```bash
./start.sh reload
```

### Monitoring the Process
For a live view of all switches and their current provisioning step:
```bash
./start.sh watch
```

## Troubleshooting

### Common Issues
- **DHCP Not Working**: Ensure no other DHCP server is active on the network and check if `network_mode: host` is being used in `docker-compose.yaml`.
- **Switch Not Downloading Config**: Check `./start.sh events` to see if the serial number was recognized and if the manifest was served correctly.
- **Reaching the Server**: Ensure `ZTP_SERVER_IP` in `.env` or `docker-compose.yaml` matches the IP of your host interface.

### Logs
- Follow container stdout: `./start.sh logs`
- View specific ZTP events: `./start.sh events`
