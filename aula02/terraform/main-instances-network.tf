provider "google" {
  project = var.project
  region  = var.region
  zone    = var.zone
}

resource "google_compute_network" "vpc_custom" {
  name                    = var.network_name
  auto_create_subnetworks = false
}

resource "google_compute_subnetwork" "subnet_custom" {
  name          = var.subnet_name
  ip_cidr_range = var.subnet_range
  region        = var.region
  network       = google_compute_network.vpc_custom.id
}

resource "google_compute_address" "internal_ip" {
  name         = "ip-interno-estatico"
  address_type = "INTERNAL"
  subnetwork   = google_compute_subnetwork.subnet_custom.id
  region       = var.region
}

resource "google_compute_router" "router_custom" {
  name    = "router-terraform"
  region  = var.region
  network = google_compute_network.vpc_custom.id
}

resource "google_compute_router_nat" "nat_custom" {
  name                               = "nat-terraform"
  router                             = google_compute_router.router_custom.name
  region                             = var.region
  nat_ip_allocate_option             = "AUTO_ONLY"
  source_subnetwork_ip_ranges_to_nat = "ALL_SUBNETWORKS_ALL_IP_RANGES"
}

resource "google_compute_firewall" "allow_ssh" {
  name    = "allow-ssh"
  network = google_compute_network.vpc_custom.name

  allow {
    protocol = "tcp"
    ports    = ["22"]
  }

  source_ranges = ["0.0.0.0/0"]
}

resource "google_compute_firewall" "allow_http" {
  name    = "allow-http"
  network = google_compute_network.vpc_custom.name

  allow {
    protocol = "tcp"
    ports    = ["80"]
  }

  source_ranges = ["0.0.0.0/0"]
}

resource "google_compute_instance" "vm_lab" {
  name         = "terraform-vm-lab"
  machine_type = "e2-micro"
  zone         = var.zone

  boot_disk {
    initialize_params {
      image = "debian-cloud/debian-12"
    }
  }

  network_interface {
    subnetwork = google_compute_subnetwork.subnet_custom.id
    network_ip = google_compute_address.internal_ip.address

    access_config {} # <- mantém IP EXTERNO
  }

  metadata_startup_script = "sudo apt update && sudo apt install -y nginx"
}
