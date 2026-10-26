import asyncio
import base64
import csv
import json
import os
import queue
import re
import shutil
import sys
import sysconfig
import threading
import unicodedata
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

# ---------------------------------------------------------------- GTK
try:
    import gi
except ModuleNotFoundError:
    raiz_stdlib = Path(sysconfig.get_path("stdlib"))
    for caminho_gtk in (
        raiz_stdlib / "dist-packages",
        raiz_stdlib.parent / "python3" / "dist-packages",
    ):
        if caminho_gtk.is_dir():
            sys.path.append(str(caminho_gtk))
    try:
        import gi
    except ModuleNotFoundError as erro:
        raise RuntimeError(
            "Instale python3-gi e gir1.2-gtk-3.0 para o Python do sistema."
        ) from erro


gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk, Pango

from copilot import CopilotClient
from copilot.session_events import AssistantMessageData

# ---------------------------------------------------------------- CAMINHOS
PASTA_APLICACAO = Path(os.path.dirname(os.path.abspath(__file__)))
PASTA_CONFIG = Path.home() / ".config" / "descrever-imagens"
FICHEIRO_CONFIG = PASTA_CONFIG / "config.json"

MIME_POR_EXTENSAO = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".webp": "image/webp",
}

NARRATIVAS = [
    ("objetiva", "Descritiva / Objetiva",
     "Descreva apenas o que é visível, de forma neutra e precisa."),
    ("poetica", "Poética",
     "Use linguagem evocativa, imagens e ritmo, sem inventar factos."),
    ("jornalistica", "Jornalística",
     "Tom de legenda de fotojornalismo: facto, contexto visível, precisão."),
    ("tecnica", "Técnica / Documental",
     "Descreva enquadramento, luz, plano e elementos como num registo técnico."),
    ("narrativa", "Narrativa (Storytelling)",
     "Conte uma micro-história sugerida pela cena, sem afirmar factos ocultos."),
    ("publicitaria", "Publicitária / Comercial",
     "Texto curto e apelativo, destacando o elemento mais forte da imagem."),
    ("cientifica", "Científica / Académica",
     "Registo formal e observável, como uma anotação de arquivo."),
    ("humoristica", "Humorística / Irónica",
     "Tom leve e bem-humorado, sem ofender pessoas visíveis."),
    ("emocional", "Emocional / Sensorial",
     "Valorize atmosferas, sensações e expressões visíveis."),
    ("historica", "Histórica",
     "Tom de catálogo de arquivo; descreva indícios visíveis de época."),
]

ASPECTOS_FOTOGRAFIA = [
    ("pessoas", "Pessoas"), ("paisagem", "Paisagem / Cenário"),
    ("cores", "Cores dominantes"), ("objetos", "Objetos em destaque"),
    ("emocoes", "Emoções / Sentimentos"), ("iluminacao", "Iluminação"),
    ("composicao", "Composição"), ("animais", "Animais"),
    ("arquitetura", "Arquitetura / Edifícios"), ("comida", "Comida / Bebidas"),
    ("eventos", "Eventos / Celebrações"), ("texto", "Texto / Documentos"),
]

CONFIG_PADRAO = {
    "ia_ativa": "copilot",
    "ias": {
        "copilot": {
            "tipo": "copilot", "nome": "GitHub Copilot",
            "base_url": "", "modelo": "", "api_key": "",
        }
    },
    "ficheiro_categorias": "categories.json",
    "formato_exportacao": "csv",
    "narrativa_padrao": "objetiva",
    "max_caracteres": 200,
    "fonte": "",
    "esquema_cores": "sistema",
}


# ---------------------------------------------------------------- CONFIG
def carregar_config() -> dict:
    PASTA_CONFIG.mkdir(parents=True, exist_ok=True)
    if FICHEIRO_CONFIG.exists():
        try:
            with open(FICHEIRO_CONFIG, encoding="utf-8") as f:
                dados = json.load(f)
            config = {**CONFIG_PADRAO, **dados}
            config["ias"] = dados.get("ias") or CONFIG_PADRAO["ias"]
            return config
        except Exception:
            pass
    # Migração: aproveitar um categories.json antigo junto ao script
    antigo = PASTA_APLICACAO / "categories.json"
    destino = PASTA_CONFIG / "categories.json"
    if antigo.exists() and not destino.exists():
        shutil.copy2(antigo, destino)
    guardar_config(CONFIG_PADRAO)
    return CONFIG_PADRAO.copy()


def guardar_config(config: dict):
    PASTA_CONFIG.mkdir(parents=True, exist_ok=True)
    with open(FICHEIRO_CONFIG, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
    os.chmod(FICHEIRO_CONFIG, 0o600)  # pode conter chaves de API


def caminho_categorias(config: dict) -> str:
    nome = config.get("ficheiro_categorias", "categories.json")
    p = Path(nome)
    return str(p if p.is_absolute() else PASTA_CONFIG / p.name)


def normalizar(obj):
    """Remove espaços finais nas chaves/valores do JSON de categorias."""
    if isinstance(obj, dict):
        return {str(k).strip(): normalizar(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [normalizar(v) for v in obj]
    if isinstance(obj, str):
        return obj.strip()
    return obj


def slugificar(texto: str) -> str:
    texto = unicodedata.normalize("NFKD", texto)
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "_", texto.lower()).strip("_")


def carregar_categorias(caminho: str) -> list:
    with open(caminho, encoding="utf-8") as f:
        dados = normalizar(json.load(f))
    grupos = dados.get("groups", [])
    raw = dados.get("categories", [])
    if not isinstance(grupos, list) or not grupos:
        raise ValueError("O ficheiro precisa de uma lista 'groups' não vazia.")
    if not isinstance(raw, list) or not raw:
        raise ValueError("O ficheiro precisa de uma lista 'categories' não vazia.")

    mapa_grupos = {}
    for g in grupos:
        if not isinstance(g, dict) or not g.get("id") or not g.get("name"):
            raise ValueError("Grupo inválido em categories.json.")
        mapa_grupos[g["id"]] = g["name"]

    categorias, vistos = [], set()
    for c in raw:
        if not isinstance(c, dict):
            raise ValueError("Categoria inválida em categories.json.")
        nome = c.get("name", "")
        grupo = c.get("groupId", "")
        if not nome or grupo not in mapa_grupos:
            raise ValueError(f"Categoria '{nome}' com groupId inválido: '{grupo}'")
        identificador = slugificar(nome)
        if not identificador or identificador in vistos:
            raise ValueError(f"Nome de categoria inválido ou duplicado: '{nome}'")
        vistos.add(identificador)
        categorias.append({
            "id": identificador, "name": nome,
            "description": c.get("description", ""),
            "grupo": mapa_grupos[grupo],
        })
    return categorias


# ---------------------------------------------------------------- PROMPT / IA
def construir_instrucao(max_caracteres, narrativa_id, foco, categorias) -> str:
    _, nome_narrativa, instrucao_narrativa = next(
        (n for n in NARRATIVAS if n[0] == narrativa_id), NARRATIVAS[0]
    )
    opcoes = "\n".join(
        f"- {c['id']}: {c['name']} ({c['grupo']}) — {c['description']}"
        for c in categorias
    )
    texto = (
        "Analisa a imagem anexada e responde apenas com um objeto JSON válido, "
        "sem cercas de código nem texto adicional, neste formato:\n"
        '{"titulo":"título curto","descricao":"descrição","categorias":["id"]}\n\n'
        f"1) Título curto e claro, apenas com base no que é visível.\n"
        f"2) Descrição em português, no máximo {max_caracteres} caracteres.\n"
        f"   Estilo narrativo: {nome_narrativa}. {instrucao_narrativa}\n"
        "3) Uma ou mais categorias, usando apenas os IDs abaixo. "
        "Inclui pelo menos um ID; não inventes IDs.\n\n"
        f"Categorias disponíveis:\n{opcoes}"
    )
    if foco.strip():
        texto += f"\n\nNa descrição, privilegia estes aspetos: {foco.strip()}."
    return texto


def interpretar_resposta(texto: str, ids_validos: set, nome: str):
    texto = texto.strip()
    if texto.startswith("```"):
        texto = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", texto).strip()
    try:
        resultado = json.loads(texto)
    except json.JSONDecodeError as erro:
        raise RuntimeError(f"A IA não devolveu JSON válido para {nome}.") from erro
    if not isinstance(resultado, dict):
        raise RuntimeError(f"Resposta inválida para {nome}.")
    titulo, descricao = resultado.get("titulo"), resultado.get("descricao")
    cats = resultado.get("categorias")
    if not isinstance(titulo, str) or not titulo.strip():
        raise RuntimeError(f"Título vazio para {nome}.")
    if not isinstance(descricao, str) or not descricao.strip():
        raise RuntimeError(f"Descrição vazia para {nome}.")
    if not isinstance(cats, list):
        raise RuntimeError(f"Falta a lista de categorias para {nome}.")
    validas = list(dict.fromkeys(
        c for c in cats if isinstance(c, str) and c in ids_validos
    ))
    if not validas:
        raise RuntimeError(f"Nenhuma categoria válida para {nome}.")
    return titulo.strip(), descricao.strip(), validas


def limitar_caracteres(texto: str, limite: int) -> str:
    if len(texto) <= limite:
        return texto
    if limite <= 1:
        return "…"
    trecho = texto[:limite - 1].rstrip()
    if " " in trecho:
        trecho = trecho.rsplit(" ", 1)[0]
    return f"{trecho or texto[:limite - 1].rstrip()}…"


def chamar_openai_compativel(ia: dict, instrucao: str, b64: str, mime: str) -> str:
    """Cliente mínimo para endpoints compatíveis com /chat/completions."""
    base = (ia.get("base_url") or "").rstrip("/")
    if not base or not ia.get("modelo"):
        raise RuntimeError("Defina a URL base e o modelo na configuração da IA.")
    payload = {
        "model": ia["modelo"],
        "temperature": 0.4,
        "max_tokens": 1200,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": instrucao},
                {"type": "image_url",
                 "image_url": {"url": f"data:{mime};base64,{b64}"}},
            ],
        }],
    }
    cabecalhos = {"Content-Type": "application/json"}
    if ia.get("api_key"):
        cabecalhos["Authorization"] = f"Bearer {ia['api_key']}"
    pedido = urllib.request.Request(
        base + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers=cabecalhos, method="POST",
    )
    try:
        with urllib.request.urlopen(pedido, timeout=300) as resposta:
            dados = json.loads(resposta.read().decode("utf-8"))
        return dados["choices"][0]["message"]["content"]
    except urllib.error.HTTPError as erro:
        raise RuntimeError(f"Erro da API ({erro.code}): {erro.read().decode()[:300]}")
    except (KeyError, IndexError, ValueError) as erro:
        raise RuntimeError("Resposta da API incompatível com o formato esperado.") from erro


async def gerar_descricoes(fotos, max_caracteres, narrativa, foco, config,
                           notificar_progresso: Callable) -> list:
    fotos = sorted(fotos, key=lambda p: os.path.basename(p).casefold())
    categorias = carregar_categorias(caminho_categorias(config))
    ids_validos = {c["id"] for c in categorias}
    instrucao_base = construir_instrucao(max_caracteres, narrativa, foco, categorias)

    ia_id = config.get("ia_ativa", "copilot")
    ia = config.get("ias", {}).get(ia_id)
    if not ia:
        raise RuntimeError(f"IA '{ia_id}' não configurada.")
    tipo = ia.get("tipo", "copilot")

    cliente = None
    if tipo == "copilot":
        ambiente = {k: v for k, v in os.environ.items()
                    if k not in {"COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"}}
        cliente = CopilotClient(env=ambiente, use_logged_in_user=True)
        await cliente.__aenter__()
        try:
            auth = await cliente.get_auth_status()
            if not auth.isAuthenticated:
                raise RuntimeError(
                    "Copilot não autenticado. Execute 'gh auth login' e confirme "
                    "o acesso ao Copilot."
                    + (f" {auth.statusMessage}" if auth.statusMessage else "")
                )
        except Exception:
            await cliente.__aexit__(None, None, None)
            raise

    resultados = []
    try:
        for indice, caminho in enumerate(fotos, start=1):
            nome = os.path.basename(caminho)
            with open(caminho, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("ascii")
            mime = MIME_POR_EXTENSAO.get(os.path.splitext(caminho)[1].lower())
            if not mime:
                raise ValueError(f"Formato não suportado: {nome}")

            if tipo == "copilot":
                async with await cliente.create_session() as session:
                    resposta = await session.send_and_wait(
                        instrucao_base,
                        attachments=[{"type": "blob", "data": b64,
                                      "mimeType": mime, "displayName": nome}],
                        timeout=300,
                    )
                if resposta is None or not isinstance(resposta.data, AssistantMessageData):
                    raise RuntimeError(f"Sem resposta para {nome}.")
                conteudo = resposta.data.content
            elif tipo == "openai":
                conteudo = await asyncio.get_event_loop().run_in_executor(
                    None, chamar_openai_compativel, ia, instrucao_base, b64, mime
                )
            else:
                raise RuntimeError(f"Tipo de IA não suportado: {tipo}")

            titulo, descricao, cats = interpretar_resposta(conteudo, ids_validos, nome)
            resultados.append({
                "ficheiro": nome, "caminho": caminho,
                "titulo": titulo,
                "descricao": limitar_caracteres(descricao, max_caracteres),
                "categorias": cats,
            })
            notificar_progresso(indice, len(fotos), nome)
    finally:
        if cliente is not None:
            await cliente.__aexit__(None, None, None)
    return resultados


def exportar_resultados(resultados, caminho_saida: str, formato: str):
    resultados = sorted(resultados, key=lambda r: r["ficheiro"].casefold())
    caminho_saida = os.path.abspath(caminho_saida)
    Path(caminho_saida).parent.mkdir(parents=True, exist_ok=True)
    if formato == "json":
        com_campos = [{k: r[k] for k in ("ficheiro", "titulo", "descricao", "categorias")}
                      for r in resultados]
        with open(caminho_saida, "w", encoding="utf-8") as f:
            json.dump(com_campos, f, indent=2, ensure_ascii=False)
    else:
        with open(caminho_saida, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["Ficheiro", "Título", "Descricao", "Categorias"])
            for r in resultados:
                writer.writerow([r["ficheiro"], r["titulo"], r["descricao"],
                                 ";".join(r["categorias"])])


# ---------------------------------------------------------------- DIÁLOGOS AUXILIARES
def miniatura(caminho, largura=110, altura=75):
    try:
        return GdkPixbuf.Pixbuf.new_from_file_at_scale(
            caminho, largura, altura, True
        )
    except Exception:
        return None


class DialogoPrompt(Gtk.Dialog):
    def __init__(self, parent, texto):
        super().__init__(title="Prompt gerado", transient_for=parent, modal=True)
        self.set_default_size(760, 520)
        self.add_buttons("_Copiar", Gtk.ResponseType.APPLY, "_Fechar", Gtk.ResponseType.CLOSE)
        caixa = self.get_content_area()
        caixa.set_border_width(10)
        nota = Gtk.Label(label="A imagem é anexada separadamente em cada pedido.")
        nota.set_xalign(0)
        caixa.pack_start(nota, False, False, 6)
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.texto = Gtk.TextView()
        self.texto.set_editable(False)
        self.texto.set_wrap_mode(Gtk.WrapMode.WORD)
        self.texto.get_buffer().set_text(texto)
        scrolled.add(self.texto)
        caixa.pack_start(scrolled, True, True, 0)
        self.connect("response", self._resposta)
        self.show_all()

    def _resposta(self, _d, resposta):
        if resposta == Gtk.ResponseType.APPLY:
            inicio, fim = self.texto.get_buffer().get_bounds()
            Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(
                self.texto.get_buffer().get_text(inicio, fim, True), -1
            )
        else:
            self.destroy()


class DialogoIA(Gtk.Dialog):
    def __init__(self, parent, nome="", dados=None):
        super().__init__(title="Configurar IA", transient_for=parent, modal=True)
        self.set_default_size(520, 320)
        self.add_buttons("_Cancelar", Gtk.ResponseType.CANCEL,
                         "_Guardar", Gtk.ResponseType.OK)
        dados = dados or {}
        grelha = Gtk.Grid()
        grelha.set_column_spacing(10)
        grelha.set_row_spacing(10)
        grelha.set_border_width(12)
        self.get_content_area().add(grelha)

        self.entrada_nome = Gtk.Entry()
        self.entrada_nome.set_text(dados.get("nome", nome))
        self.combo_tipo = Gtk.ComboBoxText()
        self.combo_tipo.append("copilot", "GitHub Copilot (CLI)")
        self.combo_tipo.append("openai", "API compatível com OpenAI")
        self.combo_tipo.set_active_id(dados.get("tipo", "copilot"))
        self.entrada_url = Gtk.Entry()
        self.entrada_url.set_text(dados.get("base_url", ""))
        self.entrada_url.set_placeholder_text("https://servidor/v1")
        self.entrada_modelo = Gtk.Entry()
        self.entrada_modelo.set_text(dados.get("modelo", ""))
        self.entrada_chave = Gtk.Entry()
        self.entrada_chave.set_visibility(False)
        self.entrada_chave.set_text(dados.get("api_key", ""))

        for linha, (rotulo, widget) in enumerate([
            ("Nome:", self.entrada_nome), ("Tipo:", self.combo_tipo),
            ("URL base:", self.entrada_url), ("Modelo:", self.entrada_modelo),
            ("Chave de API:", self.entrada_chave),
        ]):
            label = Gtk.Label(label=rotulo)
            label.set_xalign(0)
            grelha.attach(label, 0, linha, 1, 1)
            widget.set_hexpand(True)
            grelha.attach(widget, 1, linha, 1, 1)
        nota = Gtk.Label(
            label="A chave é guardada localmente em config.json (permissões 600)."
        )
        nota.get_style_context().add_class("dim-label")
        nota.set_xalign(0)
        grelha.attach(nota, 0, 5, 2, 1)
        self.show_all()

    def obter_dados(self):
        return {
            "tipo": self.combo_tipo.get_active_id() or "copilot",
            "nome": self.entrada_nome.get_text().strip() or "Nova IA",
            "base_url": self.entrada_url.get_text().strip(),
            "modelo": self.entrada_modelo.get_text().strip(),
            "api_key": self.entrada_chave.get_text().strip(),
        }


class DialogoConfiguracoes(Gtk.Dialog):
    def __init__(self, parent, config):
        super().__init__(title="Configurações", transient_for=parent, modal=True)
        self.set_default_size(620, 660)
        self.set_resizable(True)
        self.add_buttons("_Cancelar", Gtk.ResponseType.CANCEL,
                         "_Guardar", Gtk.ResponseType.OK)
        self.config = json.loads(json.dumps(config))
        self.set_border_width(10)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.get_content_area().pack_start(scrolled, True, True, 0)
        caixa = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        scrolled.add(caixa)

        # --- IA ---
        frame = Gtk.Frame(label="Inteligência Artificial")
        interior = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        interior.set_border_width(10)
        frame.add(interior)
        linha = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        linha.pack_start(Gtk.Label(label="IA ativa:"), False, False, 0)
        self.combo_ia = Gtk.ComboBoxText()
        for chave, valor in self.config["ias"].items():
            self.combo_ia.append(chave, valor.get("nome", chave))
        self.combo_ia.set_active_id(self.config.get("ia_ativa"))
        linha.pack_start(self.combo_ia, True, True, 0)
        interior.pack_start(linha, False, False, 0)

        botoes = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        for texto, funcao in (
            ("Adicionar / Editar…", self.editar_ia),
            ("Remover", self.remover_ia),
        ):
            b = Gtk.Button(label=texto)
            b.connect("clicked", funcao)
            botoes.pack_start(b, False, False, 0)
        interior.pack_start(botoes, False, False, 0)
        caixa.pack_start(frame, False, False, 0)

        # --- Categorias ---
        frame = Gtk.Frame(label="Categorias")
        interior = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        interior.set_border_width(10)
        frame.add(interior)
        self.entrada_cat = Gtk.Entry()
        self.entrada_cat.set_text(self.config.get("ficheiro_categorias", "categories.json"))
        self.entrada_cat.set_hexpand(True)
        linha = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        linha.pack_start(self.entrada_cat, True, True, 0)
        b = Gtk.Button(label="Importar e validar…")
        b.connect("clicked", self.importar_categorias)
        linha.pack_start(b, False, False, 0)
        interior.pack_start(linha, False, False, 0)
        nota = Gtk.Label(label="O ficheiro importado é copiado para ~/.config/descrever-imagens/")
        nota.get_style_context().add_class("dim-label")
        nota.set_xalign(0)
        interior.pack_start(nota, False, False, 0)
        caixa.pack_start(frame, False, False, 0)

        # --- Exportação ---
        frame = Gtk.Frame(label="Exportação de resultados")
        interior = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        interior.set_border_width(10)
        frame.add(interior)
        self.radio_csv = Gtk.RadioButton.new_with_label(None, "CSV")
        self.radio_json = Gtk.RadioButton.new_with_label_from_widget(self.radio_csv, "JSON")
        if self.config.get("formato_exportacao") == "json":
            self.radio_json.set_active(True)
        interior.pack_start(self.radio_csv, False, False, 0)
        interior.pack_start(self.radio_json, False, False, 0)
        caixa.pack_start(frame, False, False, 0)

        # --- Aparência ---
        frame = Gtk.Frame(label="Aparência")
        interior = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        interior.set_border_width(10)
        frame.add(interior)

        linha = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        linha.pack_start(Gtk.Label(label="Tipo de letra:"), False, False, 0)
        self.botao_fonte = Gtk.FontButton()
        if self.config.get("fonte"):
            self.botao_fonte.set_font(self.config["fonte"])
        linha.pack_start(self.botao_fonte, True, True, 0)
        interior.pack_start(linha, False, False, 0)

        linha = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        linha.pack_start(Gtk.Label(label="Cores:"), False, False, 0)
        self.radio_preto = Gtk.RadioButton.new_with_label(None, "Black")
        self.radio_branco = Gtk.RadioButton.new_with_label_from_widget(self.radio_preto, "White")
        self.radio_sistema = Gtk.RadioButton.new_with_label_from_widget(self.radio_preto, "Sistema")
        esquema = self.config.get("esquema_cores", "sistema")
        {"black": self.radio_preto, "white": self.radio_branco,
         "sistema": self.radio_sistema}.get(esquema, self.radio_sistema).set_active(True)
        for r in (self.radio_preto, self.radio_branco, self.radio_sistema):
            linha.pack_start(r, False, False, 0)
        interior.pack_start(linha, False, False, 0)
        caixa.pack_start(frame, False, False, 0)

        self.show_all()

    def editar_ia(self, _b):
        chave = self.combo_ia.get_active_id()
        dados = self.config["ias"].get(chave, {})
        dialogo = DialogoIA(self, dados.get("nome", ""), dados)
        if dialogo.run() == Gtk.ResponseType.OK:
            novo = dialogo.obter_dados()
            nova_chave = slugificar(novo["nome"]) or "ia"
            if chave and chave != nova_chave and chave in self.config["ias"]:
                del self.config["ias"][chave]
            self.config["ias"][nova_chave] = novo
            self.config["ia_ativa"] = nova_chave
            self._preencher_combo()
        dialogo.destroy()

    def remover_ia(self, _b):
        chave = self.combo_ia.get_active_id()
        if not chave or len(self.config["ias"]) <= 1:
            return
        del self.config["ias"][chave]
        self.config["ia_ativa"] = next(iter(self.config["ias"]))
        self._preencher_combo()

    def _preencher_combo(self):
        self.combo_ia.remove_all()
        for chave, valor in self.config["ias"].items():
            self.combo_ia.append(chave, valor.get("nome", chave))
        self.combo_ia.set_active_id(
            self.config["ia_ativa"] if self.config["ia_ativa"] in self.config["ias"]
            else next(iter(self.config["ias"]))
        )

    def importar_categorias(self, _b):
        dialogo = Gtk.FileChooserDialog(
            title="Importar categorias", transient_for=self, modal=True,
            action=Gtk.FileChooserAction.OPEN,
        )
        dialogo.add_buttons("_Cancelar", Gtk.ResponseType.CANCEL,
                     "_Importar", Gtk.ResponseType.OK),
        filtro = Gtk.FileFilter()
        filtro.set_name("JSON")
        filtro.add_pattern("*.json")
        dialogo.add_filter(filtro)
        if dialogo.run() == Gtk.ResponseType.OK:
            origem = dialogo.get_filename()
            try:
                categorias = carregar_categorias(origem)
                destino = PASTA_CONFIG / "categories.json"
                shutil.copy2(origem, destino)
                self.config["ficheiro_categorias"] = destino.name
                self.entrada_cat.set_text(destino.name)
                self._aviso(self, f"{len(categorias)} categorias importadas com sucesso.")
            except Exception as erro:
                self._aviso(self, str(erro), erro=True)
        dialogo.destroy()

    @staticmethod
    def _aviso(parent, mensagem, erro=False):
        d = Gtk.MessageDialog(transient_for=parent, modal=True,
                              message_type=Gtk.MessageType.ERROR if erro else Gtk.MessageType.INFO,
                              buttons=Gtk.ButtonsType.OK, text="Categorias")
        d.format_secondary_text(mensagem)
        d.run()
        d.destroy()

    def obter_config(self) -> dict:
        config = json.loads(json.dumps(self.config))
        config["ia_ativa"] = self.combo_ia.get_active_id() or next(iter(config["ias"]))
        config["ficheiro_categorias"] = self.entrada_cat.get_text().strip() or "categories.json"
        config["formato_exportacao"] = "json" if self.radio_json.get_active() else "csv"
        config["fonte"] = self.botao_fonte.get_font()
        config["esquema_cores"] = (
            "black" if self.radio_preto.get_active() else
            "white" if self.radio_branco.get_active() else "sistema"
        )
        return config


# ---------------------------------------------------------------- JANELA OBSERVAR
class JanelaObservacao(Gtk.Dialog):
    """Grelha editável. Só devolve OK quando o utilizador clica em Concluir."""

    def __init__(self, parent, resultados, ids_validos):
        super().__init__(title="Observar e rever descrições",
                         transient_for=parent, modal=True)
        self.set_default_size(1150, 650)
        self.set_resizable(True)
        self.ids_validos = ids_validos
        self.add_buttons("_Cancelar", Gtk.ResponseType.CANCEL,
                         "_Concluir", Gtk.ResponseType.OK)
        botao_concluir = self.get_widget_for_response(Gtk.ResponseType.OK)
        if botao_concluir:
            botao_concluir.get_style_context().add_class("suggested-action")
        self.set_border_width(10)

        nota = Gtk.Label(
            label="Clique duas vezes no título, na descrição ou nas categorias para editar. "
                  "Categorias: IDs separados por ';'."
        )
        nota.set_xalign(0)
        self.get_content_area().pack_start(nota, False, False, 6)

        # 0 miniatura | 1 ficheiro | 2 título | 3 descrição | 4 categorias | 5 caminho
        self.modelo = Gtk.ListStore(GdkPixbuf.Pixbuf, str, str, str, str, str)
        for r in sorted(resultados, key=lambda x: x["ficheiro"].casefold()):
            self.modelo.append([
                miniatura(r["caminho"]), r["ficheiro"], r["titulo"],
                r["descricao"], ";".join(r["categorias"]), r["caminho"],
            ])

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.get_content_area().pack_start(scrolled, True, True, 0)

        self.arvore = Gtk.TreeView(model=self.modelo)
        self.arvore.set_rules_hint(True)
        scrolled.add(self.arvore)

        coluna = Gtk.TreeViewColumn("Imagem / ficheiro")
        pix = Gtk.CellRendererPixbuf()
        pix.set_property("xalign", 0.5)
        coluna.pack_start(pix, False)
        coluna.add_attribute(pix, "pixbuf", 0)
        txt = Gtk.CellRendererText()
        txt.set_property("ellipsize", Pango.EllipsizeMode.END)
        txt.set_property("max-width-chars", 22)
        coluna.pack_start(txt, True)
        coluna.add_attribute(txt, "text", 1)
        coluna.set_min_width(200)
        self.arvore.append_column(coluna)

        for titulo, indice, largura in (
            ("Título", 2, 240), ("Descrição", 3, 420), ("Categorias", 4, 220),
        ):
            renderer = Gtk.CellRendererText()
            renderer.set_property("editable", True)
            renderer.set_property("wrap-mode", Pango.WrapMode.WORD_CHAR)
            renderer.set_property("max-width-chars", max(20, largura // 8))
            renderer.connect("edited", self._editado, indice)
            coluna = Gtk.TreeViewColumn(titulo, renderer, text=indice)
            coluna.set_min_width(largura)
            coluna.set_expand(titulo == "Descrição")
            self.arvore.append_column(coluna)

        self.show_all()

    def _editado(self, _renderer, caminho_str, novo_valor, indice):
        it = self.modelo.get_iter(caminho_str)
        self.modelo.set(it, indice, novo_valor)

    def obter_resultados_validados(self) -> list:
        resultados = []
        for linha in self.modelo:
            _pix, ficheiro, titulo, descricao, cats, caminho = linha[:6]
            if not titulo.strip():
                raise ValueError(f"O título de '{ficheiro}' não pode estar vazio.")
            if not descricao.strip():
                raise ValueError(f"A descrição de '{ficheiro}' não pode estar vazia.")
            ids = [c.strip() for c in cats.split(";") if c.strip()]
            invalidos = [c for c in ids if c not in self.ids_validos]
            if not ids or invalidos:
                raise ValueError(
                    f"Categorias inválidas em '{ficheiro}': "
                    + (", ".join(invalidos) if invalidos else "nenhuma selecionada")
                )
            resultados.append({
                "ficheiro": ficheiro, "caminho": caminho,
                "titulo": titulo.strip(), "descricao": descricao.strip(),
                "categorias": list(dict.fromkeys(ids)),
            })
        return resultados


# ---------------------------------------------------------------- APP
CSS_ESCURO = """
window, dialog, frame, box, grid, flowbox, popover, menu {
    background-color: #1e1e1e; color: #eeeeee;
}
label { color: #eeeeee; }
button { background-color: #333333; color: #eeeeee; }
entry, textview, textview text, treeview, list, listbox, listboxrow,
.view, scrollbar, viewport, scrolledwindow {
    background-color: #262626; color: #eeeeee;
}
treeview:selected, listboxrow:selected { background-color: #3a6ea5; color: #ffffff; }
progressbar trough { background-color: #333333; }
progressbar progress { background-color: #3a6ea5; }
"""
CSS_CLARO = """
window, dialog, frame, box, grid, flowbox, popover, menu {
    background-color: #ffffff; color: #111111;
}
label { color: #111111; }
entry, textview, textview text, treeview, list, listbox, listboxrow,
.view, scrollbar, viewport, scrolledwindow {
    background-color: #ffffff; color: #111111;
}
"""


class DescreverImagensApp(Gtk.Window):
    def __init__(self):
        super().__init__(title="Descrever Imagens")
        self.set_default_size(880, 760)
        self.set_position(Gtk.WindowPosition.CENTER)
        self.set_border_width(15)

        self.config = carregar_config()
        self.fotos = []
        self.resultados = []
        self.eventos = queue.Queue()
        self.em_execucao = False
        self.provider_estilo = None

        self._aplicar_estilo()
        self._construir_interface()
        self.connect("delete-event", self.ao_fechar)
        GLib.timeout_add(100, self._verificar_eventos)

    # ---------------- estilo
    def _aplicar_estilo(self):
        ecras = Gdk.Display.get_default()
        if ecras is None:
            return
        ecra = ecras.get_default_screen()
        if self.provider_estilo is not None:
            Gtk.StyleContext.remove_provider_for_screen(ecra, self.provider_estilo)
        provider = Gtk.CssProvider()
        css = ""
        esquema = self.config.get("esquema_cores", "sistema")
        if esquema == "black":
            css += CSS_ESCURO
        elif esquema == "white":
            css += CSS_CLARO
        fonte = self.config.get("fonte")
        if fonte:
            try:
                desc = Pango.FontDescription.from_string(fonte)
                familia = desc.get_family().replace('"', "")
                pontos = desc.get_size() / Pango.SCALE
                css += f'* {{ font-family: "{familia}"; font-size: {pontos:.1f}pt; }}'
            except Exception:
                pass
        if css:
            try:
                provider.load_from_data(css.encode("utf-8"))
                Gtk.StyleContext.add_provider_for_screen(
                    ecra, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
                )
                self.provider_estilo = provider
            except Exception:
                pass
        Gtk.Settings.get_default().set_property(
            "gtk-application-prefer-dark-theme", esquema == "black"
        )

    # ---------------- interface
    def _construir_interface(self):
        principal = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.add(principal)

        titulo = Gtk.Label()
        titulo.set_markup("<span size='large' weight='bold'>🖼️ Descrever Imagens</span>")
        titulo.set_xalign(0)
        principal.pack_start(titulo, False, False, 0)
        self.subtitulo = Gtk.Label(label="")
        self.subtitulo.set_xalign(0)
        self.subtitulo.get_style_context().add_class("dim-label")
        principal.pack_start(self.subtitulo, False, False, 0)
        self._atualizar_subtitulo()

        # 1. Imagens
        frame = Gtk.Frame(label="1. Selecione as imagens")
        caixa = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        caixa.set_border_width(10)
        frame.add(caixa)
        botoes = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.botao_adicionar = Gtk.Button(label="Adicionar imagens…")
        self.botao_adicionar.connect("clicked", self.selecionar_imagens)
        self.botao_remover = Gtk.Button(label="Remover selecionadas")
        self.botao_remover.connect("clicked", self.remover_imagens)
        self.botao_limpar = Gtk.Button(label="Limpar lista")
        self.botao_limpar.connect("clicked", self.limpar_imagens)
        for b in (self.botao_adicionar, self.botao_remover, self.botao_limpar):
            botoes.pack_start(b, False, False, 0)
        caixa.pack_start(botoes, False, False, 0)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_min_content_height(110)
        scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.lista_imagens = Gtk.ListBox()
        self.lista_imagens.set_selection_mode(Gtk.SelectionMode.MULTIPLE)
        scrolled.add(self.lista_imagens)
        caixa.pack_start(scrolled, True, True, 0)
        self.info_imagens = Gtk.Label(label="Nenhuma imagem selecionada.")
        self.info_imagens.set_xalign(0)
        caixa.pack_start(self.info_imagens, False, False, 0)
        principal.pack_start(frame, True, True, 0)

        # 2. Opções
        frame = Gtk.Frame(label="2. Configure a descrição")
        grelha = Gtk.Grid()
        grelha.set_column_spacing(10)
        grelha.set_row_spacing(8)
        grelha.set_border_width(10)
        frame.add(grelha)

        # Máximo + narrativa NA MESMA LINHA
        grelha.attach(Gtk.Label(label="Máx. caracteres (≤500):"), 0, 0, 1, 1)
        self.spin_max = Gtk.SpinButton.new_with_range(1, 500, 10)
        self.spin_max.set_value(int(self.config.get("max_caracteres", 200)))
        self.spin_max.set_width_chars(5)
        grelha.attach(self.spin_max, 1, 0, 1, 1)
        grelha.attach(Gtk.Label(label="Narrativa:"), 2, 0, 1, 1)
        self.combo_narrativa = Gtk.ComboBoxText()
        for chave, nome, _ in NARRATIVAS:
            self.combo_narrativa.append(chave, nome)
        self.combo_narrativa.set_active_id(self.config.get("narrativa_padrao", "objetiva"))
        self.combo_narrativa.set_hexpand(True)
        grelha.attach(self.combo_narrativa, 3, 0, 1, 1)

        # Aspetos
        rotulo = Gtk.Label(label="Aspetos a\nprivilegiar:")
        rotulo.set_valign(Gtk.Align.START)
        grelha.attach(rotulo, 0, 1, 1, 1)
        caixa_foco = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        grelha.attach(caixa_foco, 1, 1, 3, 1)
        atalhos = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.botao_tudo = Gtk.Button(label="Selecionar tudo")
        self.botao_tudo.connect("clicked", lambda _b: self._marcar_aspetos(True))
        self.botao_nenhum = Gtk.Button(label="Limpar seleção")
        self.botao_nenhum.connect("clicked", lambda _b: self._marcar_aspetos(False))
        atalhos.pack_start(self.botao_tudo, False, False, 0)
        atalhos.pack_start(self.botao_nenhum, False, False, 0)
        caixa_foco.pack_start(atalhos, False, False, 0)

        self.flow_aspetos = Gtk.FlowBox()
        self.flow_aspetos.set_max_children_per_line(5)
        self.flow_aspetos.set_selection_mode(Gtk.SelectionMode.NONE)
        self.flow_aspetos.set_homogeneous(True)
        self.checks_aspetos = {}
        for chave, nome in ASPECTOS_FOTOGRAFIA:
            check = Gtk.CheckButton(label=nome)
            self.checks_aspetos[chave] = check
            self.flow_aspetos.add(check)
        caixa_foco.pack_start(self.flow_aspetos, False, False, 0)

        # Saída (sem escolha de formato)
        grelha.attach(Gtk.Label(label="Guardar resultados em:"), 0, 2, 1, 1)
        self.entrada_saida = Gtk.Entry()
        self.entrada_saida.set_hexpand(True)
        self._definir_caminho_saida()
        grelha.attach(self.entrada_saida, 1, 2, 2, 1)
        self.botao_saida = Gtk.Button(label="Procurar…")
        self.botao_saida.connect("clicked", self.selecionar_saida)
        grelha.attach(self.botao_saida, 3, 2, 1, 1)

        self.botao_prompt = Gtk.Button(label="💬 Ver prompt gerado")
        self.botao_prompt.connect("clicked", self.ver_prompt)
        grelha.attach(self.botao_prompt, 0, 3, 2, 1)
        principal.pack_start(frame, False, False, 0)

        self.progresso = Gtk.ProgressBar()
        self.progresso.set_show_text(True)
        principal.pack_start(self.progresso, False, False, 0)
        self.estado = Gtk.Label(label="Selecione uma ou mais imagens.")
        self.estado.set_xalign(0)
        principal.pack_start(self.estado, False, False, 0)

        # BARRA INFERIOR: configurações à ESQUERDA, restantes à DIREITA
        barra = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.botao_config = Gtk.Button(label="⚙️ Configurações")
        self.botao_config.connect("clicked", self.abrir_configuracoes)
        barra.pack_start(self.botao_config, False, False, 0)

        direita = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.botao_fechar = Gtk.Button(label="Fechar")
        self.botao_fechar.connect("clicked", self.ao_fechar)
        self.botao_iniciar = Gtk.Button(label="Descrever imagens")
        self.botao_iniciar.get_style_context().add_class("suggested-action")
        self.botao_iniciar.connect("clicked", self.iniciar)
        self.botao_observar = Gtk.Button(label="👁 Observar")
        self.botao_observar.set_sensitive(False)  # INICIALMENTE INATIVO
        self.botao_observar.connect("clicked", self.observar)
        for b in (self.botao_fechar, self.botao_iniciar, self.botao_observar):
            direita.pack_start(b, False, False, 0)
        barra.pack_end(direita, False, False, 0)
        principal.pack_start(barra, False, False, 0)

        self._atualizar_estado_botoes()

    def _atualizar_subtitulo(self):
        nome = self.config.get("ias", {}).get(
            self.config.get("ia_ativa"), {}
        ).get("nome", "sem IA")
        self.subtitulo.set_text(f"IA ativa: {nome}  •  Configuração: {PASTA_CONFIG}")

    def _definir_caminho_saida(self):
        extensao = "json" if self.config.get("formato_exportacao") == "json" else "csv"
        base = os.path.splitext(self.entrada_saida.get_text())[0] if hasattr(self, "entrada_saida") and self.entrada_saida.get_text() else str(PASTA_CONFIG / "resultados")
        self.entrada_saida.set_text(f"{base}.{extensao}")

    def _marcar_aspetos(self, ativo):
        for check in self.checks_aspetos.values():
            check.set_active(ativo)

    def _foco_selecionado(self):
        return ", ".join(nome for chave, nome in ASPECTOS_FOTOGRAFIA
                         if self.checks_aspetos[chave].get_active())

    def _parametros(self):
        return (
            min(500, max(1, self.spin_max.get_value_as_int())),
            self.combo_narrativa.get_active_id() or "objetiva",
            self._foco_selecionado(),
        )

    def _categorias_atual(self):
        return carregar_categorias(caminho_categorias(self.config))

    # ---------------- prompt
    def ver_prompt(self, _b):
        try:
            categorias = self._categorias_atual()
        except Exception as erro:
            self.mostrar_erro(str(erro))
            return
        max_carac, narrativa, foco = self._parametros()
        texto = construir_instrucao(max_carac, narrativa, foco, categorias)
        DialogoPrompt(self, texto).run()

    # ---------------- configurações
    def abrir_configuracoes(self, _b):
        dialogo = DialogoConfiguracoes(self, self.config)
        while True:
            resposta = dialogo.run()
            if resposta != Gtk.ResponseType.OK:
                break
            try:
                novo = dialogo.obter_config()
                carregar_categorias(caminho_categorias(novo))  # valida
            except Exception as erro:
                self.mostrar_erro(str(erro))
                continue
            self.config = novo
            guardar_config(self.config)
            self._aplicar_estilo()
            self._atualizar_subtitulo()
            self._definir_caminho_saida()
            break
        dialogo.destroy()

    # ---------------- imagens
    def selecionar_imagens(self, _b):
        d = Gtk.FileChooserDialog(
            title="Selecionar imagens", transient_for=self, modal=True,
            action=Gtk.FileChooserAction.OPEN,
        )
        d.add_buttons("_Cancelar", Gtk.ResponseType.CANCEL, "_Selecionar", Gtk.ResponseType.OK)
        d.set_select_multiple(True)
        filtro = Gtk.FileFilter()
        filtro.set_name("Imagens")
        for padrao in ("*.jpg", "*.jpeg", "*.png", "*.gif", "*.webp"):
            filtro.add_pattern(padrao)
            filtro.add_pattern(padrao.upper())
        d.add_filter(filtro)
        if d.run() == Gtk.ResponseType.OK:
            caminhos = d.get_filenames()
            existentes = set(self.fotos)
            self.fotos += [c for c in caminhos if c not in existentes]
            self._atualizar_lista()
        d.destroy()

    def remover_imagens(self, _b):
        indices = {l.get_index() for l in self.lista_imagens.get_selected_rows()}
        self.fotos = [c for i, c in enumerate(self.fotos) if i not in indices]
        self._atualizar_lista()

    def limpar_imagens(self, _b):
        self.fotos.clear()
        self._atualizar_lista()

    def _atualizar_lista(self):
        for linha in self.lista_imagens.get_children():
            self.lista_imagens.remove(linha)
        self.fotos.sort(key=lambda p: os.path.basename(p).casefold())
        for caminho in self.fotos:
            linha = Gtk.ListBoxRow()
            caixa = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            caixa.set_margin_top(4)
            caixa.set_margin_bottom(4)
            nome = Gtk.Label(label=os.path.basename(caminho))
            nome.set_xalign(0)
            caixa.pack_start(nome, False, False, 0)
            local = Gtk.Label(label=os.path.dirname(caminho))
            local.set_xalign(0)
            local.set_ellipsize(Pango.EllipsizeMode.END)
            caixa.pack_start(local, True, True, 0)
            linha.add(caixa)
            self.lista_imagens.add(linha)
        self.lista_imagens.show_all()
        self.info_imagens.set_text(
            f"{len(self.fotos)} imagem(ns) selecionada(s)." if self.fotos
            else "Nenhuma imagem selecionada."
        )
        self._atualizar_estado_botoes()

    def selecionar_saida(self, _b):
        formato = self.config.get("formato_exportacao", "csv")
        d = Gtk.FileChooserDialog(
            title="Guardar resultados", transient_for=self, modal=True,
            action=Gtk.FileChooserAction.SAVE,
        )
        d.add_buttons("_Cancelar", Gtk.ResponseType.CANCEL,"_Guardar", Gtk.ResponseType.OK),
        d.set_do_overwrite_confirmation(True)
        d.set_current_name(f"resultados.{formato}")
        if d.run() == Gtk.ResponseType.OK:
            caminho = d.get_filename()
            if not caminho.lower().endswith(f".{formato}"):
                caminho += f".{formato}"
            self.entrada_saida.set_text(caminho)
        d.destroy()

    # ---------------- execução
    def iniciar(self, _b):
        if not self.fotos:
            self.mostrar_erro("Selecione pelo menos uma imagem.")
            return
        if not self.entrada_saida.get_text().strip():
            self.mostrar_erro("Indique o ficheiro de resultados.")
            return
        try:
            self._categorias_atual()
        except Exception as erro:
            self.mostrar_erro(f"Erro nas categorias: {erro}")
            return

        max_carac, narrativa, foco = self._parametros()
        self.config["max_caracteres"] = max_carac
        self.config["narrativa_padrao"] = narrativa
        guardar_config(self.config)

        self.em_execucao = True
        self.resultados = []
        self.botao_observar.set_sensitive(False)
        self._definir_estado_campos(False)
        self.progresso.set_fraction(0)
        self.progresso.set_text(f"0/{len(self.fotos)}")
        fotos, config = self.fotos.copy(), json.loads(json.dumps(self.config))

        def executar():
            try:
                resultados = asyncio.run(gerar_descricoes(
                    fotos, max_carac, narrativa, foco, config,
                    lambda i, t, n: self.eventos.put(("progresso", i, t, n)),
                ))
                self.eventos.put(("concluido", resultados))
            except Exception as erro:
                self.eventos.put(("erro", str(erro)))

        threading.Thread(target=executar, daemon=True).start()

    def observar(self, _b):
        if not self.resultados:
            return
        try:
            ids = {c["id"] for c in self._categorias_atual()}
        except Exception as erro:
            self.mostrar_erro(str(erro))
            return

        dialogo = JanelaObservacao(self, self.resultados, ids)
        while True:
            resposta = dialogo.run()
            if resposta != Gtk.ResponseType.OK:   # CANCELAR: não grava nada
                break
            try:
                resultados = dialogo.obter_resultados_validados()
                exportar_resultados(
                    resultados, self.entrada_saida.get_text().strip(),
                    self.config.get("formato_exportacao", "csv"),
                )
            except Exception as erro:
                self.mostrar_erro(str(erro))
                continue          # mantém a janela aberta para corrigir
            self.resultados = resultados
            self.mostrar_info(f"Resultados gravados em:\n{self.entrada_saida.get_text()}")
            break
        dialogo.destroy()

    # ---------------- estado / eventos
    def _definir_estado_campos(self, ativo):
        for w in (self.botao_adicionar, self.botao_remover, self.botao_limpar,
                  self.spin_max, self.combo_narrativa, self.botao_tudo,
                  self.botao_nenhum, self.entrada_saida, self.botao_saida,
                  self.botao_prompt, self.botao_iniciar, self.botao_config):
            w.set_sensitive(ativo)
        for check in self.checks_aspetos.values():
            check.set_sensitive(ativo)
        self._atualizar_estado_botoes()

    def _atualizar_estado_botoes(self):
        self.botao_remover.set_sensitive(bool(self.fotos) and not self.em_execucao)
        self.botao_limpar.set_sensitive(bool(self.fotos) and not self.em_execucao)
        self.botao_iniciar.set_sensitive(bool(self.fotos) and not self.em_execucao)
        self.botao_observar.set_sensitive(bool(self.resultados) and not self.em_execucao)

    def _verificar_eventos(self):
        while True:
            try:
                evento = self.eventos.get_nowait()
            except queue.Empty:
                break
            if evento[0] == "progresso":
                _, i, total, nome = evento
                self.progresso.set_fraction(i / total)
                self.progresso.set_text(f"{i}/{total}")
                self.estado.set_text(f"{i}/{total} concluída: {nome}")
            elif evento[0] == "erro":
                self.em_execucao = False
                self._definir_estado_campos(True)
                self.estado.set_text("O processamento terminou com um erro.")
                self.mostrar_erro(evento[1])
            elif evento[0] == "concluido":
                self.em_execucao = False
                self.resultados = evento[1]
                self._definir_estado_campos(True)
                self.estado.set_text(
                    "Concluído. Clique em Observar para rever e gravar os resultados."
                )
        return True

    def mostrar_erro(self, mensagem):
        self._mostrar_dialogo(mensagem, True)

    def _mostrar_dialogo(self, mensagem, erro=False):
        d = Gtk.MessageDialog(
            transient_for=self, modal=True,
            message_type=Gtk.MessageType.ERROR if erro else Gtk.MessageType.INFO,
            buttons=Gtk.ButtonsType.OK, text="Erro" if erro else "Concluído",
        )
        d.format_secondary_text(mensagem)
        d.run()
        d.destroy()

    def mostrar_info(self, mensagem):
        self._mostrar_dialogo(mensagem, False)

    def ao_fechar(self, *_):
        if self.em_execucao:
            self._mostrar_dialogo(
                "Aguarde o fim do processamento antes de fechar.", True
            )
            return True
        self.destroy()
        return True


if __name__ == "__main__":
    Gtk.init()
    janela = DescreverImagensApp()
    janela.connect("destroy", Gtk.main_quit)
    janela.show_all()
    Gtk.main()