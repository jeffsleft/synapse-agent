import modal
import os
from pydantic import BaseModel

# 1. Define exactly what the iPad/Terminal is sending
class Query(BaseModel):
    url: str

app = modal.App("synapse-agent")

image = modal.Image.debian_slim().pip_install(
    "google-genai", 
    "notion-client==3.0.0", 
    "requests",
    "fastapi[standard]"
)

@app.function(image=image, secrets=[modal.Secret.from_name("project-synapse")])
@modal.fastapi_endpoint(method="POST")
def process_link(query: Query): # Changed from 'data: dict' to 'query: Query'
    from google import genai
    from notion_client import Client
    import requests

    try:
        url = query.url
        print(f"🚀 Processing: {url}")

        # 1. Scrape via Jina
        reader_url = f"https://r.jina.ai/{url}"
        headers = {"Authorization": f"Bearer {os.environ['JINA_API_KEY']}"}
        content_res = requests.get(reader_url, headers=headers)
        raw_content = content_res.text[:10000] # Limit size for safety

        # 2. Think with Flash (Reliable on Free Tier)
        client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
        
        system_prompt = """
        You are an expert operator and strategist. 
        Analyze the following podcast transcript and produce a detailed structured output.
        
        Requirements:
        - Do NOT oversimplify. Preserve examples and stories.
        - Extract non-obvious insights only.
        - Use clear, structured thinking.
        
        Output sections:
        ## Executive Summary (SCR: Situation, Complication, Resolution)
        ## Epiphanies / Learnings (Include explanation, why it matters, and concrete examples)
        ## Core Concepts (Define clearly + when to apply)
        ## Action Items (Immediate, 30-90 days, Long-term)
        ## Follow-up Questions
        ## Controversial Opinions [Label [Unverified] if speculative]
        ## 3 'If True' Scenarios (12-36 months)
        ## Personal Reflection Prompts
        
        At the end include:
        Source: {{URL}}
        """

        response = client.models.generate_content(
            model="gemini-2.0-flash", 
            contents=[system_prompt, f"Source Content: {raw_content}"]
        )
        synthesis = response.text

        # 3. Write to Notion
        notion = Client(auth=os.environ["NOTION_TOKEN"])
        new_page = notion.pages.create(
            parent={"database_id": os.environ["NOTION_DATABASE_ID"]},
            properties={
                "Name": {"title": [{"text": {"content": f"Synapse: {url[:40]}"}}]},
                "URL": {"url": url}
            },
            markdown=synthesis # 2026 Notion SDK supports direct markdown!
        )

        return {"status": "success", "notion_url": new_page.get("url")}

    except Exception as e:
        print(f"❌ Error: {str(e)}")
        return {"status": "error", "message": str(e)}

    # Updated April 2026