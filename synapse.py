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

        # 1. Scrape via Jina (Capture Headers for MetaData)
        reader_url = f"https://r.jina.ai/{url}"
        headers = {"Authorization": f"Bearer {os.environ['JINA_API_KEY']}"}
        content_res = requests.get(reader_url, headers=headers)
        
        # Extract Title and Date from Jina's custom headers
        page_title = content_res.headers.get("x-respond-title", "Untitled Source")
        # Jina doesn't always have the date, so we'll use 'Today' as a fallback
        page_date = datetime.now().strftime("%Y-%m-%d") 
        
        raw_content = content_res.text[:10000]

        # 2. Think with Flash
        client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
        
        system_prompt = f"""
        You are an expert operator and strategist. 
        Analyze the following content and produce a detailed structured output.
        
        Requirements:
        - Do NOT oversimplify. Preserve examples and stories.
        - Extract non-obvious insights only.
        
        Output sections:
        ## Executive Summary (SCR: Situation, Complication, Resolution)
        ## Epiphanies / Learnings
        ## Core Concepts
        ## Action Items
        ## Follow-up Questions
        ## Controversial Opinions
        ## 3 'If True' Scenarios
        ## Personal Reflection Prompts
        
        At the end include:
        Source: {page_title} | Date: {page_date} | URL: {url}
        """

        response = client.models.generate_content(
            model="gemini-2.0-flash", 
            contents=[system_prompt, f"Source Content: {raw_content}"]
        )
        synthesis = response.text

        # 3. Write to Notion (Using the new Metadata)
        notion = Client(auth=os.environ["NOTION_TOKEN"])
        new_page = notion.pages.create(
            parent={"database_id": os.environ["NOTION_DATABASE_ID"]},
            properties={
                "Name": {"title": [{"text": {"content": page_title}}]},
                "URL": {"url": url}
            },
            markdown=synthesis 
        )

        return {"status": "success", "notion_url": new_page.get("url")}

    except Exception as e:
        print(f"❌ Error: {str(e)}")
        return {"status": "error", "message": str(e)}

    # Updated 12 April 2026