"""Tool: create_order — creates/updates an order in CRM pipeline."""

CREATE_ORDER_TOOL: dict = {
    "type": "function",
    "function": {
        "name": "create_order",
        "description": (
            "Cria um pedido no CRM para o contato atual. "
            "Use quando o cliente confirmar que quer comprar/fechar pedido "
            "E os dados obrigatórios já estiverem completos (nome, endereço, CPF e data de nascimento)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Resumo curto do pedido (produto/servico principal).",
                },
                "notes": {
                    "type": "string",
                    "description": "Detalhes do pedido combinados com o cliente.",
                },
                "potential_value": {
                    "type": "number",
                    "description": "Valor estimado do pedido em reais (opcional).",
                },
            },
            "required": [],
            "additionalProperties": False,
        },
    },
}
