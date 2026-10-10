# Harness Engineering: explained from zero

This synthetic fixture distinguishes two durable subjects. Harness Engineering is the practice of
designing and improving an Agent Harness. An Agent Harness is the operational system that gives an
AI agent instructions, tools, feedback, and durable memory so it can complete a task reliably.

Robin Hale (@robinhale_example) wrote this explanation. A model alone is not an agentic system:
the model reads text and proposes the next step, and the harness is what carries that step out. The explainer
groups the harness into six capabilities that serve the model and three that make the agent
trustworthy for the people who use it. The capabilities are: (1) tools let the model request named
actions; (2) a loop runs decide, act, observe, and decide again; (3) memory/state keeps work outside
the stateless model; (4) context selection decides what the model sees on each call; (5) a working
environment or isolated workspace contains execution; and (6) a clear objective and verification
turn "done" into acceptance criteria checked outside the model.

The trust capabilities are: (7) permissions and limits decide which tools may run and which need
approval; (8) observability keeps traces of context, calls, parameters, and outcomes; and (9) evals
run stable evaluation tasks to compare harness versions and catch regressions.

Skills, MCP, subagents, and long-term memory extend the same harness rather than replace the pattern
in which the model asks and the harness executes.

The source post identifier is 1000000000000000042.

OpenAI built a large internal software product with one million lines of code and 1,500 pull requests
through an agent harness rather than by changing the underlying model. LangChain measured its own
coding agent rising from rank 30 to the top 5 on Terminal Bench 2.0 after improving the harness
without changing the model. Anthropic documented that the same model can produce either a polished
but broken application or a working app under different harness configurations. These results
support the explainer's main conclusion.

Claude Code and Codex are examples of coding-agent environments. ReAct is a prompting pattern,
not an identity to file as a person or organization.
