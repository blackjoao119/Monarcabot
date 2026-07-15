import asyncio
import re
import logging
from typing import Optional

from telebot.async_telebot import AsyncTeleBot
from telebot.types import Message
from playwright.async_api import async_playwright, Page, Browser, TimeoutError as PlaywrightTimeout

# -------------------- CONFIGURAÇÕES --------------------
TOKEN_TELEGRAM = "SEU_TOKEN_AQUI"          # Token do bot no Telegram
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
VIEWPORT = {"width": 1366, "height": 768}
TIMEOUT_PAGINA = 45000                     # 45s para carregamento geral
TIMEOUT_ELEMENTO = 10000                   # 10s para botões aparecerem
MAX_TENTATIVAS = 2                         # Retry em caso de falha

# -------------------- LOGGING --------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


# -------------------- EXCEÇÕES PERSONALIZADAS --------------------
class ExtracaoErro(Exception):
    """Erro genérico na extração."""
    pass

class CloudflareBloqueio(ExtracaoErro):
    """Detectado bloqueio explícito do Cloudflare."""
    pass

class ChaveNaoEncontrada(ExtracaoErro):
    """A chave FREE_* não foi localizada em nenhuma estratégia."""
    pass

class TimeoutCarregamento(ExtracaoErro):
    """Timeout ao aguardar página ou elemento."""
    pass


# -------------------- EXTRATOR DE CHAVES --------------------
class KeyExtractor:
    """
    Gerencia uma instância do navegador Playwright com evasão de detecção manual.
    Oferece um método assíncrono para percorrer o fluxo do LootLabs e extrair a chave.
    """

    def __init__(self):
        self.browser: Optional[Browser] = None
        self.playwright = None

    async def iniciar(self):
        """Lançar o navegador com configurações anti-detecção."""
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-infobars",
                "--disable-background-timer-throttling",
                "--disable-backgrounding-occluded-windows",
                "--disable-renderer-backgrounding",
            ]
        )
        logger.info("Navegador iniciado com sucesso.")

    async def fechar(self):
        """Encerrar navegador e recursos."""
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()
        logger.info("Navegador finalizado.")

    async def extrair_chave(self, url: str) -> str:
        """
        Lógica principal de extração.
        - Cria um contexto isolado com fingerprint crível.
        - Navega pelo fluxo de redirecionamento e cliques.
        - Aplica fallback para encontrar o padrão FREE_.
        """
        if not self.browser:
            raise ExtracaoErro("Navegador não iniciado. Chame iniciar() primeiro.")

        contexto = await self.browser.new_context(
            user_agent=USER_AGENT,
            viewport=VIEWPORT,
            locale="pt-BR",
            timezone_id="America/Sao_Paulo",
            permissions=["geolocation"],
            geolocation={"latitude": -23.5505, "longitude": -46.6333},
        )
        pagina = await contexto.new_page()

        # Aplica stealth manual (substituto do playwright_stealth)
        await self._aplicar_stealth_manual(pagina)

        try:
            # Tentativa com retry
            for tentativa in range(1, MAX_TENTATIVAS + 1):
                logger.info(f"Tentativa {tentativa}/{MAX_TENTATIVAS} para {url}")
                try:
                    chave = await self._processar_pagina(pagina, url)
                    if chave:
                        return chave
                    else:
                        logger.warning("Chave vazia retornada, tentando novamente...")
                except (TimeoutCarregamento, CloudflareBloqueio) as e:
                    logger.error(f"Falha na tentativa {tentativa}: {e}")
                    if tentativa == MAX_TENTATIVAS:
                        raise  # repassa a exceção final
                    await asyncio.sleep(2)  # pausa antes de retry
                finally:
                    # Limpa cookies e storage para nova tentativa
                    await contexto.clear_cookies()
                    await pagina.goto("about:blank")

            raise ChaveNaoEncontrada("Todas as tentativas de extração falharam.")
        finally:
            await contexto.close()

    async def _aplicar_stealth_manual(self, pagina: Page):
        """
        Injeção manual de scripts anti-detecção para eliminar a dependência
        do pacote playwright_stealth. Esconde rastros de automação.
        """
        await pagina.add_init_script("""
            // 1. Remover flag webdriver
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined,
            });

            // 2. Sobrescrever plugins
            Object.defineProperty(navigator, 'plugins', {
                get: () => [1, 2, 3, 4, 5],
            });

            // 3. Sobrescrever languages
            Object.defineProperty(navigator, 'languages', {
                get: () => ['pt-BR', 'pt'],
            });

            // 4. Adicionar objeto chrome
            window.chrome = { runtime: {} };

            // 5. Permissões: evitar detecção por Notification.permission
            const originalQuery = window.navigator.permissions.query;
            window.navigator.permissions.query = (parameters) => (
                parameters.name === 'notifications' ?
                Promise.resolve({ state: Notification.permission }) :
                originalQuery(parameters)
            );

            // 6. Sobrescrever o cabeçalho Accept-Language via fetch (se necessário)
            const originalFetch = window.fetch;
            window.fetch = function(url, options = {}) {
                if (!options.headers) {
                    options.headers = {};
                }
                options.headers['Accept-Language'] = 'pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7';
                return originalFetch.call(this, url, options);
            };
        """)
        logger.info("Stealth manual aplicado à página.")

    async def _processar_pagina(self, pagina: Page, url: str) -> Optional[str]:
        """
        Etapas internas:
        1. Acessar a URL.
        2. Aguardar carregamento e redirecionamentos.
        3. Interagir com botões (Get Link, Free Access etc.) até surgir a chave.
        4. Aplicar múltiplas estratégias de extração.
        """
        # Navegação inicial
        try:
            await pagina.goto(url, wait_until="domcontentloaded", timeout=TIMEOUT_PAGINA)
        except PlaywrightTimeout:
            raise TimeoutCarregamento("Timeout ao carregar a página inicial.")

        # Aguarda a rede ficar ociosa e scripts executarem (cronômetro)
        await pagina.wait_for_load_state("networkidle", timeout=TIMEOUT_PAGINA)

        # Verifica se fomos barrados pelo Cloudflare explicitamente
        if await pagina.title() == "Just a moment...":
            raise CloudflareBloqueio("Bloqueio do Cloudflare detectado (página de verificação).")

        # Loop de interação: procura e clica em botões até não encontrar mais ou a chave aparecer
        while True:
            # Tenta extrair antes de qualquer clique (caso a chave já esteja visível)
            chave = await self._extrair_com_fallback(pagina)
            if chave:
                return chave

            # Identifica o botão de "continuar" (textos comuns no LootLabs)
            botao = await self._encontrar_botao_continuar(pagina)
            if not botao:
                # Se não há botão e também não temos chave, algo mudou
                raise ChaveNaoEncontrada(
                    "Nenhum botão de continuação encontrado e a chave não apareceu."
                )

            # Clica no botão e espera possíveis navegações
            try:
                async with pagina.expect_navigation(wait_until="domcontentloaded", timeout=TIMEOUT_ELEMENTO):
                    await botao.click()
            except PlaywrightTimeout:
                # Alguns sites não navegam, apenas revelam o conteúdo; ignoramos o timeout de navegação
                pass
            except Exception:
                # Se o clique falhar, tentamos forçar via JavaScript
                await botao.evaluate("el => el.click()")

            # Pequena pausa para scripts internos reagirem
            await pagina.wait_for_timeout(1500)

    async def _encontrar_botao_continuar(self, pagina: Page):
        """Localiza botão com texto típico ('Get Link', 'Free Access', 'Continue', etc.)."""
        seletores = [
            "button:has-text('Get Link')",
            "button:has-text('Free Access')",
            "button:has-text('Continue')",
            "a:has-text('Get Link')",
            "a:has-text('Free Access')",
            "a:has-text('Continue')",
            "button:has-text('Download')",
            "a:has-text('Download')",
        ]
        for seletor in seletores:
            try:
                botao = await pagina.wait_for_selector(seletor, state="visible", timeout=3000)
                if botao and await botao.is_enabled():
                    return botao
            except PlaywrightTimeout:
                continue
        return None

    async def _extrair_com_fallback(self, pagina: Page) -> Optional[str]:
        """
        Múltiplas estratégias para capturar a chave FREE_*
        1. Regex no conteúdo HTML completo.
        2. Regex no texto visível (innerText).
        3. Tenta capturar da URL atual (query params).
        4. Avalia JavaScript para buscar variáveis globais conhecidas (ex: window.link).
        """
        # Estratégia 1: HTML bruto
        html = await pagina.content()
        chave = self._aplicar_regex(html)
        if chave:
            logger.info("Chave encontrada no HTML completo.")
            return chave

        # Estratégia 2: texto visível da página
        texto = await pagina.inner_text("body")
        chave = self._aplicar_regex(texto)
        if chave:
            logger.info("Chave encontrada no texto visível.")
            return chave

        # Estratégia 3: URL atual
        url_atual = pagina.url
        chave = self._aplicar_regex(url_atual)
        if chave:
            logger.info("Chave encontrada na URL.")
            return chave

        # Estratégia 4: JS – busca em algumas variáveis globais comuns
        scripts = [
            "window.link",
            "window.finalUrl",
            "window.redirectUrl",
            "document.querySelector('input[type=text]')?.value",
            "document.querySelector('code')?.innerText",
        ]
        for script in scripts:
            try:
                valor = await pagina.evaluate(script)
                if valor and isinstance(valor, str):
                    chave = self._aplicar_regex(valor)
                    if chave:
                        logger.info(f"Chave encontrada via JS ({script}).")
                        return chave
            except Exception:
                continue

        return None

    @staticmethod
    def _aplicar_regex(texto: str) -> Optional[str]:
        """Aplica regex para FREE_ seguido de caracteres alfanuméricos."""
        match = re.search(r"FREE_[a-zA-Z0-9]+", texto)
        return match.group(0) if match else None


# -------------------- BOT DO TELEGRAM --------------------
extrator = KeyExtractor()

bot = AsyncTeleBot(TOKEN_TELEGRAM)

@bot.message_handler(commands=["start", "help"])
async def comando_start(message: Message):
    texto = (
        "🤖 *Bot Extrator de Chaves FREE_* \n\n"
        "Envie um link do LootLabs (ou similar) com o comando /key. Exemplo:\n"
        "`/key https://link.lootlabs.com/xyz`\n\n"
        "Aguarde enquanto processo a página e extraio a chave."
    )
    await bot.reply_to(message, texto, parse_mode="Markdown")

@bot.message_handler(commands=["key"])
async def comando_key(message: Message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await bot.reply_to(message, "❌ Por favor, forneça o link após o comando. Ex: /key https://...")
        return

    url = args[1].strip()
    await bot.reply_to(message, "⏳ Processando o link... Isso pode levar alguns segundos.")

    try:
        chave = await extrator.extrair_chave(url)
        await bot.reply_to(message, f"✅ Chave extraída com sucesso:\n`{chave}`", parse_mode="Markdown")
    except CloudflareBloqueio:
        await bot.reply_to(message, "🛡️ O site ativou uma verificação anti-bot (Cloudflare) que não pôde ser contornada. Tente novamente mais tarde.")
    except TimeoutCarregamento:
        await bot.reply_to(message, "⏰ O site demorou muito para responder. Verifique se o link está correto ou tente novamente.")
    except ChaveNaoEncontrada:
        await bot.reply_to(message, "🔍 A chave no formato `FREE_*` não foi encontrada. A estrutura do site pode ter mudado.")
    except Exception as e:
        logger.exception("Erro inesperado:")
        await bot.reply_to(message, f"⚠️ Ocorreu um erro interno: {type(e).__name__}. O administrador foi notificado.")

async def main():
    await extrator.iniciar()
    logger.info("Bot iniciado. Pressione Ctrl+C para parar.")
    try:
        await bot.infinity_polling()
    finally:
        await extrator.fechar()

if __name__ == "__main__":
    asyncio.run(main())                try:
                    chave = await self._processar_pagina(pagina, url)
                    if chave:
                        return chave
                    else:
                        logger.warning("Chave vazia retornada, tentando novamente...")
                except (TimeoutCarregamento, CloudflareBloqueio) as e:
                    logger.error(f"Falha na tentativa {tentativa}: {e}")
                    if tentativa == MAX_TENTATIVAS:
                        raise  # repassa a exceção final
                    await asyncio.sleep(2)  # pausa antes de retry
                finally:
                    # Limpa cookies e storage para nova tentativa
                    await contexto.clear_cookies()
                    await pagina.goto("about:blank")

            raise ChaveNaoEncontrada("Todas as tentativas de extração falharam.")
        finally:
            await contexto.close()

    async def _processar_pagina(self, pagina: Page, url: str) -> Optional[str]:
        """
        Etapas internas:
        1. Acessar a URL.
        2. Aguardar carregamento e redirecionamentos.
        3. Interagir com botões (Get Link, Free Access etc.) até surgir a chave.
        4. Aplicar múltiplas estratégias de extração.
        """
        # Navegação inicial
        try:
            await pagina.goto(url, wait_until="domcontentloaded", timeout=TIMEOUT_PAGINA)
        except PlaywrightTimeout:
            raise TimeoutCarregamento("Timeout ao carregar a página inicial.")

        # Aguarda a rede ficar ociosa e scripts executarem (cronômetro)
        await pagina.wait_for_load_state("networkidle", timeout=TIMEOUT_PAGINA)

        # Verifica se fomos barrados pelo Cloudflare explicitamente
        if await pagina.title() == "Just a moment...":
            raise CloudflareBloqueio("Bloqueio do Cloudflare detectado (página de verificação).")

        # Loop de interação: procura e clica em botões até não encontrar mais ou a chave aparecer
        while True:
            # Tenta extrair antes de qualquer clique (caso a chave já esteja visível)
            chave = await self._extrair_com_fallback(pagina)
            if chave:
                return chave

            # Identifica o botão de "continuar" (textos comuns no LootLabs)
            botao = await self._encontrar_botao_continuar(pagina)
            if not botao:
                # Se não há botão e também não temos chave, algo mudou
                raise ChaveNaoEncontrada(
                    "Nenhum botão de continuação encontrado e a chave não apareceu."
                )

            # Clica no botão e espera possíveis navegações
            try:
                async with pagina.expect_navigation(wait_until="domcontentloaded", timeout=TIMEOUT_ELEMENTO):
                    await botao.click()
            except PlaywrightTimeout:
                # Alguns sites não navegam, apenas revelam o conteúdo; ignoramos o timeout de navegação
                pass
            except Exception:
                # Se o clique falhar, tentamos forçar via JavaScript
                await botao.evaluate("el => el.click()")

            # Pequena pausa para scripts internos reagirem
            await pagina.wait_for_timeout(1500)

    async def _encontrar_botao_continuar(self, pagina: Page):
        """Localiza botão com texto típico ('Get Link', 'Free Access', 'Continue', etc.)."""
        seletores = [
            "button:has-text('Get Link')",
            "button:has-text('Free Access')",
            "button:has-text('Continue')",
            "a:has-text('Get Link')",
            "a:has-text('Free Access')",
            "a:has-text('Continue')",
            "button:has-text('Download')",
            "a:has-text('Download')",
        ]
        for seletor in seletores:
            try:
                botao = await pagina.wait_for_selector(seletor, state="visible", timeout=3000)
                if botao and await botao.is_enabled():
                    return botao
            except PlaywrightTimeout:
                continue
        return None

    async def _extrair_com_fallback(self, pagina: Page) -> Optional[str]:
        """
        Múltiplas estratégias para capturar a chave FREE_*
        1. Regex no conteúdo HTML completo.
        2. Regex no texto visível (innerText).
        3. Tenta capturar da URL atual (query params).
        4. Avalia JavaScript para buscar variáveis globais conhecidas (ex: window.link).
        """
        # Estratégia 1: HTML bruto
        html = await pagina.content()
        chave = self._aplicar_regex(html)
        if chave:
            logger.info("Chave encontrada no HTML completo.")
            return chave

        # Estratégia 2: texto visível da página
        texto = await pagina.inner_text("body")
        chave = self._aplicar_regex(texto)
        if chave:
            logger.info("Chave encontrada no texto visível.")
            return chave

        # Estratégia 3: URL atual
        url_atual = pagina.url
        chave = self._aplicar_regex(url_atual)
        if chave:
            logger.info("Chave encontrada na URL.")
            return chave

        # Estratégia 4: JS – busca em algumas variáveis globais comuns
        scripts = [
            "window.link",
            "window.finalUrl",
            "window.redirectUrl",
            "document.querySelector('input[type=text]')?.value",
            "document.querySelector('code')?.innerText",
        ]
        for script in scripts:
            try:
                valor = await pagina.evaluate(script)
                if valor and isinstance(valor, str):
                    chave = self._aplicar_regex(valor)
                    if chave:
                        logger.info(f"Chave encontrada via JS ({script}).")
                        return chave
            except Exception:
                continue

        return None

    @staticmethod
    def _aplicar_regex(texto: str) -> Optional[str]:
        """Aplica regex para FREE_ seguido de caracteres alfanuméricos."""
        match = re.search(r"FREE_[a-zA-Z0-9]+", texto)
        return match.group(0) if match else None


# -------------------- BOT DO TELEGRAM --------------------
extrator = KeyExtractor()

bot = AsyncTeleBot(TOKEN_TELEGRAM)

@bot.message_handler(commands=["start", "help"])
async def comando_start(message: Message):
    texto = (
        "🤖 *Bot Extrator de Chaves FREE_* \n\n"
        "Envie um link do LootLabs (ou similar) com o comando /key. Exemplo:\n"
        "`/key https://link.lootlabs.com/xyz`\n\n"
        "Aguarde enquanto processo a página e extraio a chave."
    )
    await bot.reply_to(message, texto, parse_mode="Markdown")

@bot.message_handler(commands=["key"])
async def comando_key(message: Message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await bot.reply_to(message, "❌ Por favor, forneça o link após o comando. Ex: /key https://...")
        return

    url = args[1].strip()
    await bot.reply_to(message, "⏳ Processando o link... Isso pode levar alguns segundos.")

    try:
        chave = await extrator.extrair_chave(url)
        await bot.reply_to(message, f"✅ Chave extraída com sucesso:\n`{chave}`", parse_mode="Markdown")
    except CloudflareBloqueio:
        await bot.reply_to(message, "🛡️ O site ativou uma verificação anti-bot (Cloudflare) que não pôde ser contornada. Tente novamente mais tarde.")
    except TimeoutCarregamento:
        await bot.reply_to(message, "⏰ O site demorou muito para responder. Verifique se o link está correto ou tente novamente.")
    except ChaveNaoEncontrada:
        await bot.reply_to(message, "🔍 A chave no formato `FREE_*` não foi encontrada. A estrutura do site pode ter mudado.")
    except Exception as e:
        logger.exception("Erro inesperado:")
        await bot.reply_to(message, f"⚠️ Ocorreu um erro interno: {type(e).__name__}. O administrador foi notificado.")

async def main():
    await extrator.iniciar()
    logger.info("Bot iniciado. Pressione Ctrl+C para parar.")
    try:
        await bot.infinity_polling()
    finally:
        await extrator.fechar()

if __name__ == "__main__":
    asyncio.run(main())
