DEFAULT_TOOL_DOCS = {
    "general_assistant": (
        "Answer from your own general knowledge. This step calls no tool for "
        "it; it marks a step that may answer directly."
    ),
    "document_search": (
        "Search the company's knowledge-base documents and the files in this "
        "chat. Use it whenever the user asks about company data, an uploaded or "
        "attached file, or anything a document could answer, including people "
        "and named things that may be in the documents. Answer only from the "
        "returned passages and cite them by their [n] number; say plainly when "
        "they do not contain the answer."
    ),
    "create_chart": (
        "Show a pie, bar or line chart in the chat. Use it when the user asks "
        "for a chart, graph or plot and you have the numbers: from earlier tool "
        "results, or from your general knowledge for well-known public figures "
        "(then add \"(general knowledge)\" to the title and say the figures are "
        "approximate). Never put chart data in your reply text; keep the reply "
        "to a short caption."
    ),
    "chart_from_file": (
        "Build a chart from a CSV file in this chat or the knowledge base by "
        "counting its real values. Use it when the user asks to chart a named "
        "CSV file or dataset."
    ),
    "chat_summarise": (
        "Return the transcript of this conversation so you can summarise it. "
        "Use it when the user asks to summarise, recap or review this chat; "
        "then write a concise plain-text summary of the key questions and "
        "conclusions."
    ),
    "web_search": (
        "Use when the user asks for web, online, current or external "
        "information. When it has no live results, carry on with your own "
        "general knowledge (chart it if a chart was asked for) with a short "
        "note that the figures are approximate; never ask the user first."
    ),
    "add_to_knowledge_base": (
        "Process this chat's files through Temporal, or stop, resume, cancel "
        "or list that processing. \"Process this file\" uses destination "
        "\"chat\": the text stays in this chat only and nothing is added to "
        "Documents. Use destination \"knowledge_base\" only when the user "
        "explicitly asks to add the file to the company knowledge base or "
        "company documents. If the wording does not say which (\"add this "
        "file\", \"upload it\"), ask the user first."
    ),
    "document_processing_status": (
        "Report the processing status of this chat's files and documents "
        "(read from Temporal, with each workflow id), or stop, resume, cancel "
        "or list their processing. Use it when the user asks what is "
        "processing, whether a file is done, for a status or a workflow id."
    ),
}
