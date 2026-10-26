# Descrever Imagens

Aplicação gráfica em Python que usa o GitHub Copilot para criar títulos,
descrições e categorias para imagens e guardar os resultados num ficheiro CSV.

## Requisitos

- Python 3.13 (ou uma versão compatível com as dependências do projeto)
- GitHub Copilot CLI instalado e autenticado (`gh auth login`)
- Dependências Python instaladas a partir de `requirements.txt`
- GTK 3 e bindings PyGObject instalados no sistema (`python3-gi` e
  `gir1.2-gtk-3.0` em Debian/Ubuntu)

## Instalação

Na pasta do projeto, crie e prepare o ambiente virtual:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

No Debian/Ubuntu, instale também os componentes gráficos do GTK 3:

```bash
sudo apt install python3-gi gir1.2-gtk-3.0
```

O ambiente virtual e os bindings GTK do sistema devem usar a mesma versão
compatível de Python.

## Utilização

Inicie a aplicação com o Python do ambiente virtual:

```bash
.venv/bin/python descrever-imagens.py
```

Na janela GTK 3, selecione uma ou mais imagens JPG, PNG, GIF ou WebP, ajuste o
limite de caracteres e, opcionalmente, indique aspetos a privilegiar. O
resultado é guardado por predefinição em `resultados.csv`, na pasta do projeto;
o destino pode ser alterado na própria aplicação.
