# 📰 feed — seu briefing diário

Agrega RSS de múltiplas fontes, filtra com IA e gera uma página visual atualizada todo dia às 8h.

---

## Setup (faça uma vez só)

### 1. Crie o repositório no GitHub

- Acesse [github.com/new](https://github.com/new)
- Nome do repositório: `feed`
- Visibilidade: **Public** (necessário para GitHub Pages gratuito)
- Clique em **Create repository**

### 2. Suba os arquivos

Via terminal (ou pelo Trae):

```bash
git init
git add .
git commit -m "🚀 initial setup"
git branch -M main
git remote add origin https://github.com/romulobrasil/feed.git
git push -u origin main
```

### 3. Pegue sua chave Gemini (gratuita)

- Acesse [aistudio.google.com/app/apikey](https://aistudio.google.com/app/apikey)
- Clique em **Create API Key**
- Copie a chave gerada

### 4. Configure o secret no GitHub

- No seu repositório, vá em **Settings → Secrets and variables → Actions**
- Clique em **New repository secret**
- Nome: `GEMINI_API_KEY`
- Valor: a chave que você copiou
- Clique em **Add secret**

### 5. Ative o GitHub Pages

- Vá em **Settings → Pages**
- Em **Source**, selecione **Deploy from a branch**
- Branch: `main` / pasta: `/ (root)`
- Clique em **Save**
- Em alguns minutos seu feed estará em: `https://romulobrasil.github.io/feed`

### 6. Rode manualmente pela primeira vez

- Vá em **Actions → Generate Daily Feed**
- Clique em **Run workflow → Run workflow**
- Aguarde ~2 minutos
- Acesse a URL e veja o resultado 🎉

---

## Personalizando

Tudo que você precisa editar está no arquivo `config.yaml`:

```yaml
# Mudar horário (padrão: 8h Brasília)
app:
  cron_hour: 8

# Adicionar uma nova categoria
categories:
  - id: games
    label: "Games"
    emoji: "🎮"
    nicho: "jogos eletrônicos, lançamentos, reviews, esports..."
    feeds:
      - name: IGN
        url: https://feeds.ign.com/ign/all

# Adicionar feed a categoria existente
  - id: tech
    feeds:
      - name: Novo Site
        url: https://exemplo.com/rss
```

### Como encontrar o RSS de um site

- Tente: `https://seusite.com/rss`, `/feed`, `/rss.xml`, `/atom.xml`
- Ou use a extensão [RSS Feed Finder](https://chrome.google.com/webstore/detail/rss-feed-finder) no Chrome

---

## Apontando domínio próprio (opcional)

Para usar `app.romulobrasil.com/feed`:

1. Vá em **Settings → Pages → Custom domain**
2. Digite `app.romulobrasil.com` e salve
3. No DNS da Hostinger, adicione um registro CNAME:
   - Nome: `app`
   - Valor: `romulobrasil.github.io`
4. Aguarde a propagação (até 24h)

---

## Estrutura do projeto

```
feed/
├── .github/
│   └── workflows/
│       └── generate.yml     # Agendamento (GitHub Actions)
├── scripts/
│   ├── generate.py          # Script principal
│   └── template.html        # Template visual
├── config.yaml              # ← Você edita aqui
├── requirements.txt         # Dependências Python
├── index.html               # Gerado automaticamente
└── README.md
```

---

## Problemas comuns

**O workflow não rodou às 8h**
> GitHub Actions com cron pode atrasar até 30min em horários de pico. Normal.

**Algumas notícias sem imagem**
> Nem todos os feeds incluem thumbnail. O card adapta o layout automaticamente.

**Categoria com poucas notícias**
> O feed do site pode ter limite de itens ou não publicou nas últimas 24h. Adicione mais fontes no `config.yaml`.

**Erro de API Gemini**
> Verifique se o secret `GEMINI_API_KEY` está correto. A geração funciona sem IA (sem destaques e sem resumo do dia).
