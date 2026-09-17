import os
import httpx
from fastmcp import FastMCP

try:
    from ddgs import DDGS
except ImportError:
    from duckduckgo_search import DDGS

mcp = FastMCP("Web-Search")

SEARXNG_URL = os.environ.get("SEARXNG_URL", "")

@mcp.tool()
async def search_web(query: str, max_results: int = 5) -> str:
    """Search the web using integrated search engine and return formatted results."""
    # 1. Intentar con SearXNG si está configurado y activo
    if SEARXNG_URL:
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                resp = await client.get(
                    f"{SEARXNG_URL}/search",
                    params={"q": query, "format": "json"}
                )
                if resp.status_code == 200:
                    data = resp.json()
                    results = data.get("results", [])[:max_results]
                    if results:
                        formatted = []
                        for i, r in enumerate(results, 1):
                            title = r.get("title", "No Title")
                            url = r.get("url", "")
                            content = r.get("content", "")
                            engine = r.get("engine", "searxng")
                            formatted.append(f"{i}. [{title}]({url}) ({engine})\n{content}")
                        return "\n\n".join(formatted)
        except Exception:
            pass

    # 2. Búsqueda directa integrada con DDGS (autónoma, rápida y sin contenedor externo)
    try:
        results = []
        with DDGS() as ddgs:
            for r in ddgs.text(query, max_results=max_results):
                results.append(r)

        if not results:
            return f"No results found for '{query}'."

        formatted = []
        for i, r in enumerate(results, 1):
            title = r.get("title", "No Title")
            url = r.get("href", r.get("url", ""))
            body = r.get("body", r.get("content", ""))
            formatted.append(f"{i}. [{title}]({url})\n{body}")

        return "\n\n".join(formatted)
    except Exception as e:
        return f"Error performing web search: {e}"

if __name__ == "__main__":
    mcp.run(transport="streamable-http", host="0.0.0.0", port=8092)
