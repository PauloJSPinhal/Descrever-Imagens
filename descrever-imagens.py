import asyncio
import argparse
import base64
import csv
import glob
import json
import os

from copilot import CopilotClient
from copilot.session_events import AssistantMessageData


def carregar_categorias():
    with open("categories.json", encoding="utf-8") as ficheiro:
        dados = json.load(ficheiro)

    if not isinstance(dados, dict):
        raise ValueError("categories.json tem de conter um objeto JSON.")

    grupos = dados.get("groups")
    if not isinstance(grupos, list) or not grupos:
        raise ValueError("categories.json tem de conter uma lista 'groups' não vazia.")

    categorias = []
    ids = set()
    for grupo in grupos:
        if not isinstance(grupo, dict):
            raise ValueError("Cada grupo em categories.json tem de ser um objeto.")

        identificador = grupo.get("id")
        nome = grupo.get("name")
        descricao = grupo.get("description")
        if not all(isinstance(valor, str) and valor.strip() for valor in (identificador, nome, descricao)):
            raise ValueError("Cada grupo precisa de 'id', 'name' e 'description' não vazios.")
        if identificador in ids:
            raise ValueError(f"ID de grupo duplicado em categories.json: {identificador}")

        ids.add(identificador)
        categorias.append(
            {
                "id": identificador,
                "name": nome,
                "description": descricao,
            }
        )

    return categorias


def interpretar_resposta(texto: str, ids_validos: set[str], nome: str):
    try:
        resultado = json.loads(texto)
    except json.JSONDecodeError as erro:
        raise RuntimeError(f"O Copilot não devolveu JSON válido para {nome}.") from erro

    if not isinstance(resultado, dict):
        raise RuntimeError(f"O Copilot devolveu um objeto inválido para {nome}.")

    titulo = resultado.get("titulo")
    descricao = resultado.get("descricao")
    categorias_resposta = resultado.get("categorias")
    if not isinstance(titulo, str) or not titulo.strip():
        raise RuntimeError(f"O Copilot devolveu um título vazio ou inválido para {nome}.")
    if not isinstance(descricao, str) or not descricao.strip():
        raise RuntimeError(f"O Copilot devolveu uma descrição vazia ou inválida para {nome}.")
    if not isinstance(categorias_resposta, list):
        raise RuntimeError(f"O Copilot não devolveu uma lista de categorias para {nome}.")

    categorias_validas = list(
        dict.fromkeys(
            identificador
            for identificador in categorias_resposta
            if isinstance(identificador, str) and identificador in ids_validos
        )
    )
    if not categorias_validas:
        raise RuntimeError(
            f"O Copilot não devolveu nenhum ID de categoria válido para {nome}."
        )

    return titulo.strip(), descricao.strip(), categorias_validas


def limitar_caracteres(texto: str, limite: int) -> str:
    if len(texto) <= limite:
        return texto
    if limite == 1:
        return "…"

    trecho = texto[: limite - 1].rstrip()
    if " " in trecho:
        trecho = trecho.rsplit(" ", 1)[0]
    if not trecho:
        trecho = texto[: limite - 1].rstrip()
    return f"{trecho}…"


def obter_argumentos():
    parser = argparse.ArgumentParser(
        description="Gera descrições de imagens e grava os resultados em resultados.csv."
    )
    parser.add_argument(
        "--max-caracteres",
        type=int,
        default=200,
        help="Limite máximo de caracteres por descrição (predefinição: 200).",
    )
    parser.add_argument(
        "--foco",
        default="",
        help="Aspetos a privilegiar ou ignorar na descrição, por exemplo: 'cores e pessoas'.",
    )
    argumentos = parser.parse_args()
    if argumentos.max_caracteres < 1:
        parser.error("--max-caracteres tem de ser pelo menos 1.")
    return argumentos


async def main(max_caracteres: int, foco: str):
    fotos = sorted(glob.glob("./minhas-fotos/*.jpg"))
    if not fotos:
        raise FileNotFoundError("Nenhuma imagem .jpg encontrada na pasta ./minhas-fotos")

    categorias = carregar_categorias()
    ids_validos = {categoria["id"] for categoria in categorias}
    opcoes_categoria = "\n".join(
        f"- {categoria['id']}: {categoria['name']} — {categoria['description']}"
        for categoria in categorias
    )

    print(f"{len(fotos)} imagem(ns) encontrada(s).")

    variaveis_de_token = {"COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"}
    ambiente_copilot = {
        chave: valor
        for chave, valor in os.environ.items()
        if chave not in variaveis_de_token
    }

    async with CopilotClient(env=ambiente_copilot, use_logged_in_user=True) as client:
        auth_status = await client.get_auth_status()
        if not auth_status.isAuthenticated:
            detalhe = f" Detalhe: {auth_status.statusMessage}" if auth_status.statusMessage else ""
            raise RuntimeError(
                "O GitHub Copilot não está autenticado no Copilot CLI. Execute "
                "'gh auth login' e confirme que a conta tem acesso ao Copilot."
                f"{detalhe}"
            )

        with open("resultados.csv", "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["Ficheiro", "Título", "Descricao", "Categorias"])

            for indice, caminho in enumerate(fotos, start=1):
                nome = os.path.basename(caminho)
                print(f"Descrevendo imagem {indice}/{len(fotos)}: {nome}")

                with open(caminho, "rb") as img:
                    imagem_base64 = base64.b64encode(img.read()).decode("ascii")

                instrucao = (
                    "Analisa a imagem e responde apenas com um objeto JSON válido, "
                    "sem blocos de código ou texto adicional, neste formato: "
                    '{"titulo":"título curto","descricao":"descrição da imagem","categorias":["id"]}. '
                    "Cria um título curto, claro e adequado à imagem, sem acrescentar factos não visíveis. "
                    f"A descrição deve estar em português e ter no máximo {max_caracteres} caracteres. "
                    "Escolhe uma ou mais categorias usando apenas os IDs abaixo. "
                    "A escolha deve corresponder à descrição de cada categoria e ao conteúdo visível; "
                    "inclui pelo menos um ID. Não inventes IDs.\n"
                    f"Categorias disponíveis:\n{opcoes_categoria}"
                )
                if foco.strip():
                    instrucao += f"\nNa descrição, dá prioridade a estes aspetos: {foco.strip()}."

                async with await client.create_session() as session:
                    resposta = await session.send_and_wait(
                        instrucao,
                        attachments=[
                            {
                                "type": "blob",
                                "data": imagem_base64,
                                "mimeType": "image/jpeg",
                                "displayName": nome,
                            }
                        ],
                        timeout=300,
                    )

                if resposta is None or not isinstance(resposta.data, AssistantMessageData):
                    raise RuntimeError(f"O Copilot não devolveu uma descrição para {nome}.")

                titulo, descricao, categorias_validas = interpretar_resposta(
                    resposta.data.content,
                    ids_validos,
                    nome,
                )
                descricao = limitar_caracteres(descricao, max_caracteres)
                writer.writerow([nome, titulo, descricao, ";".join(categorias_validas)])
                f.flush()

    print("Concluído. Descrições gravadas em resultados.csv.")


if __name__ == "__main__":
    argumentos = obter_argumentos()
    asyncio.run(main(argumentos.max_caracteres, argumentos.foco))