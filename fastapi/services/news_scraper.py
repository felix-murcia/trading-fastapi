import httpx
import logging
from xml.etree import ElementTree

logger = logging.getLogger(__name__)

async def get_macro_news(symbol: str = "EURUSD") -> str:
    """
    Extrae los ultimos 3 titulares macroeconomicos para enriquecer el RAG de Qwen.
    Usamos el RSS publico de Investing.com para Forex.
    """
    url = "https://www.investing.com/rss/news_1.rss"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            
        tree = ElementTree.fromstring(resp.text)
        news_items = []
        
        # Parse top 3 to keep token count extremely low for the local 4B model
        for item in tree.findall('.//item')[:3]:
            title = item.find('title')
            if title is not None and title.text:
                news_items.append(f"- {title.text.strip()}")
                
        if not news_items:
            return "No breaking macro news found."
            
        return "\n".join(news_items)
        
    except httpx.TimeoutException:
        logger.warning("Timeout scraping news from Investing.com")
        return "Macro data temporarily unavailable (timeout)."
    except Exception as e:
        logger.error(f"Error scraping news: {e}")
        return "Macro data unavailable."
