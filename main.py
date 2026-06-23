import operator
from typing import Annotated
from langgraph.graph import StateGraph, MessagesState, END
from dotenv import load_dotenv
import os
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain.agents import create_agent
from langchain_groq import ChatGroq
from langgraph.checkpoint.memory import MemorySaver
from pg_tools import TOOLS
from faq_tools import faq_retriever
from prompts import (
    ROUTER_PROMPT_COMPLETO,
    FINANCEIRO_PROMPT_COMPLETO,
    AGENDA_PROMPT_COMPLETO,
    ORQUESTRADOR_PROMPT_COMPLETO,
    FAQ_PROMPT,
)
from guardrail import guardrail_entrada, guardrail_saida, anonimizar_entrada, desanonimizar_saida
from langchain_core.messages import RemoveMessage

load_dotenv()

# ==============================================================================
# MODELOS
# ==============================================================================
llm_gemini = ChatGoogleGenerativeAI(
    model="gemini-2.5-flash",
    temperature=0.7,
    top_p=0.95,
    api_key=os.getenv("GEMINI_API_KEY"),
)
llm_groq = ChatGroq(
    model="openai/gpt-oss-120b",
    temperature=0.7,
    api_key=os.getenv("GROQ_API_KEY"),
)
llm_especialista = llm_gemini.with_fallbacks([llm_groq])
llm_rapido = ChatGroq(
    model="llama-3.3-70b-versatile",
    temperature=0.0,
    api_key=os.getenv("GROQ_API_KEY"),
)

# ==============================================================================
# AGENTES
# ==============================================================================
router_app       = create_agent(model=llm_rapido,       system_prompt=ROUTER_PROMPT_COMPLETO)
financeiro_app   = create_agent(model=llm_especialista, tools=TOOLS, system_prompt=FINANCEIRO_PROMPT_COMPLETO)
agenda_app       = create_agent(model=llm_especialista, system_prompt=AGENDA_PROMPT_COMPLETO)
orquestrador_app = create_agent(model=llm_rapido,       system_prompt=ORQUESTRADOR_PROMPT_COMPLETO)
faq_app          = create_agent(model=llm_rapido,       tools=[faq_retriever], system_prompt=FAQ_PROMPT)

# ==============================================================================
# ESTADO
# MessagesState já traz: messages: Annotated[list[AnyMessage], add_messages]
# O checkpointer propaga automaticamente para os subgrafos adicionados via add_node
# ==============================================================================
class Estado(MessagesState):
    agentes_chamados: Annotated[list[str], operator.add]
    mapa_pii:         dict
    rota:             str   # decisão do roteador — não polui o histórico
    


# ==============================================================================
# NÓ ROTEADOR — necessário apenas para extrair a rota sem poluir messages
# ==============================================================================
def no_roteador(estado: Estado) -> dict:
    saida = router_app.invoke({"messages": list(estado["messages"])})
    texto = saida["messages"][-1].text

    if "ROUTE=" not in texto:
        return {
            "agentes_chamados": ["roteador"],
            "rota":             "fim",
            "messages":         [{"role": "assistant", "content": texto}],
        }

    rota = "fim"
    for linha in texto.splitlines():
        if linha.startswith("ROUTE="):
            rota = linha.split("=", 1)[1].strip()
            break

    return {
        "agentes_chamados": ["roteador", rota],
        "rota":             rota,
        # Histórico limpo: o especialista vai ler só a conversa real
    }
    

# ==============================================================================
# NÓ ORQUESTRADOR — wrapper para capturar agentes_chamados via rota
# ==============================================================================

def no_orquestrador(estado: Estado) -> dict:
    # Pega a última resposta do especialista (última AIMessage com conteúdo)
    ultima_especialista = ""
    for mensagem in reversed(estado["messages"]):
        if mensagem.type == "ai" and mensagem.content:
            ultima_especialista = mensagem.content
            break
    
    saida = orquestrador_app.invoke({
        "messages": {"role": "human", "content": ultima_especialista}
    })

    return {
        "agentes_chamados": ["orquestrador"],
        "messages":        [{"role": "assistant", "content": saida["messages"][-1].text}]
    }
    
# ==============================================================================
# NÓ GUARDRAIL ENTRADA - 
# ==============================================================================
def no_guadrail_entrada(estado: Estado) -> dict:
    # 1. Salva a pergunta do usuário
    pergunta_usuario = estado["messages"][-1]
    
    # 2. Anonimizar entrada do usuário
    pergunta_anonimizada, mapa = anonimizar_entrada(pergunta_usuario.text)
    
    # 3. Envia para guardrail de entrada
    resultado = guardrail_entrada(pergunta_anonimizada)
    
    # 4. Verifica se foi bloqueado ou não:
    if resultado["bloqueado"] == True:
        return {
            "messages": [{ "role": "assistant", "content": resultado["mensagem"]}],
            "rota":     "fim"
        }
    else:
        return {
            "rota":     "roteador",                             # Opcional
            "mapa":     mapa,
            "messages": [RemoveMessage(pergunta_usuario.id), { "role": "assistant", "content": pergunta_anonimizada}],
        }
        
# ==============================================================================
# NÓ GUARDRAIL SAÍDA - 
# ==============================================================================
def no_guardrail_saida(estado: Estado) -> dict:
    # 1. Resposta do orquestrador
    
    # 2. Envio para a função:
    resultado = guardrail_saida(estado["messages"][-1], estado["mapa_pii"])
    
    return {
        "messages": [{ "role": "assistant", "content": resultado["conteudo"]}]
    }
    
# ==============================================================================
# FUNÇÃO DE DECISÃO
# ==============================================================================
def decidir_especialista(estado: Estado) -> str:
    return estado["rota"] if estado["rota"] in ("financeiro", "agenda", "faq") else "fim"

def decidir_pos_guardrail_entrada(estado: Estado) -> str:
    return estado["rota"]

# ==============================================================================
# GRAFO - Conseguimos que todos os agentes recebam todo o histórico
# ==============================================================================
grafo = StateGraph(Estado)

grafo.add_node("guardrail_entrada", no_guadrail_entrada)
grafo.add_node("roteador",          no_roteador)
grafo.add_node("financeiro",        financeiro_app)   # subgrafo direto — checkpointer propaga
grafo.add_node("agenda",            agenda_app)        # subgrafo direto
grafo.add_node("faq",               faq_app)           # subgrafo direto
grafo.add_node("orquestrador",      no_orquestrador)
grafo.add_node("guardrail_saida", no_guardrail_saida)

grafo.set_entry_point("guardrail_entrada")

grafo.add_conditional_edges(
    "guardrail_entrada",
    decidir_pos_guardrail_entrada,
    {
        "roteador":        "roteador",
        "fim":             END,
    }
)

grafo.add_conditional_edges(
    "roteador",
    decidir_especialista,
    {
        "financeiro": "financeiro",
        "agenda":     "agenda",
        "faq":        "faq",
        "fim":        END,
    },
)

grafo.add_edge("financeiro",      "orquestrador")
grafo.add_edge("agenda",          "orquestrador")
grafo.add_edge("orquestrador",    "guardrail_saida")
grafo.add_edge("faq",             END)                      # Não passa para o guardrail de saída, pois ele responde com base em um documento fixo
grafo.add_edge("guardrail_saida", END)

memory = MemorySaver()
fluxo_agentes = grafo.compile(checkpointer=memory)

# ==============================================================================
# FLUXO PRINCIPAL
# ==============================================================================
def executar_fluxo_assessor(pergunta_usuario: str, session_id: str) -> str:
    estado_inicial = {
        "messages":         [{"role": "human", "content": pergunta_usuario}],
        "agentes_chamados": [],
        "mapa":             {},
        "rota":             "",
    }       

    estado_final = fluxo_agentes.invoke(
        estado_inicial,
        config={"configurable": {"thread_id": session_id}},
    )

    print(f"[debug] agentes chamados: {estado_final['agentes_chamados']}")
    return estado_final["messages"][-1].text

# ==============================================================================
# LOOP
# ==============================================================================
while True:
    try:
        user_input = input("> ")
        if user_input.lower() in ("sair", "end", "fim", "tchau", "bye", "exit", "0", "-1"):
            print("Encerrando a conversa.")
            break

        resposta = executar_fluxo_assessor(
            pergunta_usuario=user_input,
            session_id="sessao_teste",
        )
        print(resposta)

    except Exception as e:
        print("Erro ao consumir a API:", e)
        continue