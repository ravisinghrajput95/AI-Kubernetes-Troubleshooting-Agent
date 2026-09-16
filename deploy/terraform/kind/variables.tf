variable "kubeconfig_path" {
  type = string
}

variable "kube_context" {
  type = string
}

variable "namespace" {
  type    = string
  default = "k8s-agent-tf"
}

variable "image_repository" {
  description = "A backend image already loaded into the kind node."
  type        = string
  default     = "k8s-agent-backend"
}

variable "image_tag" {
  type    = string
  default = "tfverify"
}
