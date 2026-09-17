import os
import httpx
from fastmcp import FastMCP
from bs4 import BeautifulSoup
import html2text

mcp = FastMCP("Fetch & Web Reader")

@mcp.tool()
async def fetch_url(url: str, max_chars: int = 12000) -> str:
    """Fetch content from a URL, strip boilerplate, and convert it to clean markdown."""
    async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}) as client:
        try:
            resp = await client.get(url)
            resp.raise_for_status()
            
            # Convert HTML to clean markdown
            h = html2text.HTML2Text()
            h.ignore_links = False
            h.ignore_images = True
            h.body_width = 0
            
            text = h.handle(resp.text)
            if len(text) > max_chars:
                return text[:max_chars] + f"\n\n[Truncated: {len(text) - max_chars} characters omitted]"
            return text
        except Exception as e:
            return f"Error fetching URL: {e}"

if __name__ == "__main__":
    mcp.run(transport="sse", host="0.0.0.0", port=8095)
