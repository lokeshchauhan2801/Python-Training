"""The chat Agent: one LangGraph tool-calling graph defined in code.

``graph.build_graph`` defines the graph, ``tools`` the LangChain tools it can
call, ``service.AgentChatService`` runs one chat turn through it, and
``checkpointer`` keeps each chat session's thread in Postgres.
"""
