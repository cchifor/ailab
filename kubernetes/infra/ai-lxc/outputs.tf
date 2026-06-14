output "ai_llm_containers" {
  description = "AI LLM LXCs (name => host node / vmid / IP / API URL)."
  value = {
    for k, v in var.ai_llm_nodes : k => {
      node = v.node_name
      vmid = v.vm_id
      ip   = v.ip
      api  = "http://${v.ip}:8080/v1"
    }
  }
}
