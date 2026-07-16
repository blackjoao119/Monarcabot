import re
import asyncio
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
from playwright_stealth import stealth_async  # patch para contornar Cloudflare

# --------------------------------------------------------------
# Constantes de configuração
# --------------------------------------------------------------
PADRAO_CHAVE = re.compile(r'FREE_[a-zA-Z0-9]{32}')  # Ex: FREE_ab21...28bc7
TIMEOUT_NAVEGADOR = 30000  # ms
RETRY_MAXIMO = 2          # tentativas extras além da inicial
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# --------------------------------------------------------------
# Função principal de extração (assíncrona)
# --------------------------------------------------------------
async def extrair_chave_lootlabs(url: str) -> str:
    """
    Acessa a URL, segue o fluxo do encurtador (cronômetro, botões)
    e retorna a chave FREE_... encontrada no HTML final.
    Lança exceção descritiva se falhar.
    """
    ultimo_erro = None

    for tentativa in range(1 + RETRY_MAXIMO):
        try:
            return await _tentativa_extracao(url, tentativa)
        except Exception as e:
            ultimo_erro = e
            if tentativa < RETRY_MAXIMO:
                await asyncio.sleep(2)  # pausa antes do retry
            else:
                # Esgota as tentativas – levanta a exceção
                raise RuntimeError(
                    f"Falha após {1 + RETRY_MAXIMO} tentativas. Último erro: {ultimo_erro}"
                ) from ultimo_erro

# --------------------------------------------------------------
# Lógica interna de uma única tentativa
# --------------------------------------------------------------
async def _tentativa_extracao(url: str, tentativa: int) -> str:
    async with async_playwright() as p:
        # ---- Configuração do navegador (headless otimizado) ----
        navegador = await p.chromium.launch(
            headless=True,
            args=[
                '--disable-gpu',
                '--no-sandbox',
                '--disable-dev-shm-usage',     # importante em VPS pouca RAM
                '--single-process',            # reduz consumo
                '--disable-blink-features=AutomationControlled',  # oculta flag de automação
                '--window-size=1280,720'
            ]
        )

        # Cria contexto com User-Agent realista
        contexto = await navegador.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1280, "height": 720},
            locale="en-US",
            permissions=[],  # sem permissões
        )

        pagina = await contexto.new_page()

        # Aplica o stealth logo após abrir a página
        await stealth_async(pagina)

        try:
            # 1. Navegar até o link (primeiro redirecionamento)
            await pagina.goto(url, wait_until="domcontentloaded", timeout=TIMEOUT_NAVEGADOR)

            # 2. Aguardar a execução completa da página (cronômetros e scripts)
            await pagina.wait_for_load_state("networkidle", timeout=TIMEOUT_NAVEGADOR)

            # 3. Interagir com o fluxo típico do LootLabs:
            #    → aguardar o botão "Get Link" ou "Free Access" ficar disponível
            #    → clicar e esperar nova página/redirecionamento
            await _seguir_fluxo_humano(pagina)

            # 4. Na página final, buscar a chave no HTML
            html_final = await pagina.content()
            chave = _extrair_chave_do_html(html_final)
            if chave:
                return chave

            # ---- Fallback: tentar obter a chave da URL (alguns encurtadores colocam na barra)
            url_final = pagina.url
            chave_url = _extrair_chave_do_html(url_final)
            if chave_url:
                return chave_url

            # Se não encontrou, erro específico
            raise ValueError("Estrutura do site modificada: chave não encontrada no HTML nem na URL.")

        finally:
            await navegador.close()

# --------------------------------------------------------------
# Fluxo de cliques (simula um humano)
# --------------------------------------------------------------
async def _seguir_fluxo_humano(pagina):
    """
    Aguarda e clica nos botões típicos do LootLabs, com timeouts generosos.
    Adapte os seletores se o site mudar.
    """
    # Lista de possíveis textos do botão principal (ordem de prioridade)
    botoes_esperados = [
        "button:has-text('Get Link')",
        "button:has-text('Continue')",
        "button:has-text('Free Access')",
        "button:has-text('Acessar')",
        "button:has-text('Ver Link')",
    ]

    # Tenta localizar qualquer um desses botões, espera até 15s pelo primeiro
    botao_alvo = None
    for seletor in botoes_esperados:
        try:
            botao_alvo = await pagina.wait_for_selector(seletor, timeout=15000)
            break   # encontrou, sai do loop
        except PlaywrightTimeout:
            continue

    if not botao_alvo:
        # Se não encontrou nenhum botão, pode ser que o link já tenha redirecionado direto
        # (verificamos depois na página final) – apenas aguarda um pouco mais por via das dúvidas
        await pagina.wait_for_timeout(5000)
        return

    # Clica no botão e espera a navegação (nova página ou carregamento)
    try:
        # Aguarda que o botão esteja realmente habilitado (cronômetro)
        await botao_alvo.wait_for_element_state("enabled", timeout=20000)

        # Clica e aguarda um novo estado "networkidle" ou troca de URL
        async with pagina.expect_navigation(wait_until="networkidle", timeout=TIMEOUT_NAVEGADOR):
            await botao_alvo.click()
    except PlaywrightTimeout:
        # Se não houver navegação, talvez tenha abrido um pop‑up; tentamos fechar e clicar de novo
        await _fechar_popups(pagina)
        await botao_alvo.click()
        await pagina.wait_for_load_state("networkidle", timeout=TIMEOUT_NAVEGADOR)

    # Em alguns casos, o LootLabs usa um iframe para o link final; damos tempo extra
    await pagina.wait_for_timeout(3000)

async def _fechar_popups(pagina):
    """Fecha overlays e pop‑ups comuns que bloqueiam cliques."""
    popup_selectors = [
        "button:has-text('Close')",
        "button:has-text('Fechar')",
        "[aria-label='Close']",
        ".close",
    ]
    for sel in popup_selectors:
        try:
            popup_btn = await pagina.wait_for_selector(sel, timeout=2000)
            if popup_btn:
                await popup_btn.click()
                await pagina.wait_for_timeout(500)
        except PlaywrightTimeout:
            continue

# --------------------------------------------------------------
# Extração via regex
# --------------------------------------------------------------
def _extrair_chave_do_html(conteudo: str) -> str | None:
    """Procura o padrão FREE_... no texto. Retorna a primeira ocorrência ou None."""
    match = PADRAO_CHAVE.search(conteudo)
    return match.group(0) if match else None
