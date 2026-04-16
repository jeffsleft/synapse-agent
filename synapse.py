import modal
import os
from pydantic import BaseModel

# 1. Define the environment
image = modal.Image.debian_slim().pip_install(
    "google-genai", 
    "notion-client", 
    "requests", 
    "fastapi[standard]"
)
app = modal.App("synapse-agent")

# 2. Define the data structure
class Query(BaseModel):
    url: str

# 3. The Function
@app.function(image=image, secrets=[modal.Secret.from_name("project-synapse")])
@modal.fastapi_endpoint(method="POST")
def process_link(query: Query):
    from google import genai
    from notion_client import Client
    import requests
    from datetime import datetime

    try:
        url = query.url
        print(f"🚀 Processing: {url}")

        # 1. Scrape via Jina
        reader_url = f"https://r.jina.ai/{url}"
        headers = {"Authorization": f"Bearer {os.environ['JINA_API_KEY']}"}
        content_res = requests.get(reader_url, headers=headers)
        
        page_title = content_res.headers.get("x-respond-title", "Untitled Source")
        # Capture current date for Notion
        process_date = datetime.now().strftime("%Y-%m-%d") 
        raw_content = content_res.text[:30000]

        # 2. Think with Gemini 3 Flash
        client = genai.Client(
            api_key=os.environ["GEMINI_API_KEY"],
            http_options={'api_version': 'v1beta'}
        )
        
        system_prompt = f"""
        You are an expert operator. 
        IMPORTANT: Your response must start with a # followed by a concise, punchy title. 
        DO NOT include any introductory sentences or preamble. 
        Start immediately with:
        # [Title]
        
        Analyze the following content and produce a detailed structured output.
        
        Requirements:
        - Preserve examples and stories. Extract non-obvious insights.
        
        Output sections:
        ## Executive Summary (SCR)
        ## Epiphanies / Learnings
        ## Core Concepts
        ## Action Items
        ## Follow-up Questions
        ## Controversial Opinions
        ## 3 'If True' Scenarios
        ## Personal Reflection Prompts
        
        Source: {page_title} | Date: {process_date} | URL: {url}
        """

        response = client.models.generate_content(
            model="gemini-3-flash-preview", 
            contents=[system_prompt, f"Source Content: {raw_content}"]
        )
        synthesis = response.text

        # --- 3. Logic & Cleaning ---
        lines = [l.strip() for l in synthesis.split('\n') if l.strip()]
        
        # Grab title from the first line and remove markdown #
        raw_title = lines[0].replace('#', '').strip() if lines else "New Analysis"
        generated_title = ' '.join(raw_title.split()[:12]) # Keep title punchy
        
        # Table snippet for Synthesis column
        table_synthesis = synthesis[:1500].strip() + "..."

        # Extraction for the Action Items and Scenarios columns
        action_items_snippet = "No specific actions identified."
        scenarios_snippet = "No scenarios identified."
        
        if "## Action Items" in synthesis:
            action_items_snippet = synthesis.split("## Action Items")[1].split("##")[0].strip()[:1800]
        if "## 3 'If True' Scenarios" in synthesis:
            scenarios_snippet = synthesis.split("## 3 'If True' Scenarios")[1].split("##")[0].strip()[:1800]

        # Deep-Dive Page Content
        paragraphs = synthesis.split('\n\n')
        text_blocks = []
        for p in paragraphs:
            if not p.strip(): continue
            block_type = "paragraph"
            content = p.strip()
            
            if content.startswith('##'):
                block_type = "heading_2"
                content = content.replace('##', '').strip()
            
            # Notion block character limit handling
            if len(content) > 2000:
                for chunk in [content[i:i+2000] for i in range(0, len(content), 2000)]:
                    text_blocks.append({
                        "object": "block",
                        "type": "paragraph",
                        "paragraph": {"rich_text": [{"type": "text", "text": {"content": chunk}}]}
                    })
            else:
                text_blocks.append({
                    "object": "block",
                    "type": block_type,
                    block_type: {"rich_text": [{"type": "text", "text": {"content": content}}]}
                })

        # --- 4. Write to Notion ---
        notion = Client(auth=os.environ["NOTION_TOKEN"])
        new_page = notion.pages.create(
            parent={"database_id": os.environ["NOTION_DATABASE_ID"]},
            properties={
                "Name": {"title": [{"text": {"content": generated_title}}]},
                "URL": {"url": url},
                "Date": {"date": {"start": process_date}},
                "Status": {"status": {"name": "New"}},
                "Synthesis": {"rich_text": [{"type": "text", "text": {"content": table_synthesis}}]},
                "Action Items": {"rich_text": [{"type": "text", "text": {"content": action_items_snippet}}]},
                "Key Scenarios": {"rich_text": [{"type": "text", "text": {"content": scenarios_snippet}}]}
            },
            children=text_blocks 
        )

        return {"status": "success", "notion_url": new_page.get("url")}

    except Exception as e:
        print(f"❌ Error: {str(e)}")
        return {"status": "error", "message": str(e)}