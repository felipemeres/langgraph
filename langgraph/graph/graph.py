import logging
from asyncio import iscoroutinefunction
from collections import defaultdict
from typing import Any, Callable, Dict, NamedTuple, Optional, Sequence

from langchain_core.runnables import Runnable
from langchain_core.runnables.base import (
    RunnableLambda,
    RunnableLike,
    coerce_to_runnable,
)
from langchain_core.runnables.config import RunnableConfig
from langchain_core.runnables.graph import Graph as RunnableGraph

from langgraph.checkpoint import BaseCheckpointSaver
from langgraph.pregel import Channel, Pregel

logger = logging.getLogger(__name__)

END = "__end__"


class Branch(NamedTuple):
    condition: Callable[..., str]
    ends: Optional[dict[str, str]]

    def runnable(self, input: Any) -> Runnable:
        result = self.condition(input)
        if self.ends:
            destination = self.ends[result]
        else:
            destination = result
        return Channel.write_to(f"{destination}:inbox" if destination != END else END)


class Graph:
    def __init__(self) -> None:
        self.nodes: dict[str, Runnable] = {}
        self.edges = set[tuple[str, str]]()
        self.branches: defaultdict[str, list[Branch]] = defaultdict(list)
        self.support_multiple_edges = False
        self.compiled = False

    def add_node(self, key: str, action: RunnableLike) -> None:
        if self.compiled:
            logger.warning(
                "Adding a node to a graph that has already been compiled. This will "
                "not be reflected in the compiled graph."
            )
        if key in self.nodes:
            raise ValueError(f"Node `{key}` already present.")
        if key == END:
            raise ValueError(f"Node `{key}` is reserved.")

        self.nodes[key] = coerce_to_runnable(action)

    def add_edge(self, start_key: str, end_key: str) -> None:
        if self.compiled:
            logger.warning(
                "Adding an edge to a graph that has already been compiled. This will "
                "not be reflected in the compiled graph."
            )
        if start_key == END:
            raise ValueError("END cannot be a start node")
        if start_key not in self.nodes:
            raise ValueError(f"Need to add_node `{start_key}` first")
        if end_key not in self.nodes and end_key != END:
            raise ValueError(f"Need to add_node `{end_key}` first")

        if not self.support_multiple_edges and start_key in set(
            start for start, _ in self.edges
        ):
            raise ValueError(f"Already found path for {start_key}")

        self.edges.add((start_key, end_key))

    def add_conditional_edges(
        self,
        start_key: str,
        condition: Callable[..., str],
        conditional_edge_mapping: Optional[Dict[str, str]] = None,
    ) -> None:
        if self.compiled:
            logger.warning(
                "Adding an edge to a graph that has already been compiled. This will "
                "not be reflected in the compiled graph."
            )
        if start_key not in self.nodes:
            raise ValueError(f"Need to add_node `{start_key}` first")
        if iscoroutinefunction(condition):
            raise ValueError("Condition cannot be a coroutine function")
        if conditional_edge_mapping and set(
            conditional_edge_mapping.values()
        ).difference([END]).difference(self.nodes):
            raise ValueError(
                f"Missing nodes which are in conditional edge mapping. Mapping "
                f"contains possible destinations: "
                f"{list(conditional_edge_mapping.values())}. Possible nodes are "
                f"{list(self.nodes.keys())}."
            )

        self.branches[start_key].append(Branch(condition, conditional_edge_mapping))

    def set_entry_point(self, key: str) -> None:
        if self.compiled:
            logger.warning(
                "Setting the entry point of a graph that has already been compiled. "
                "This will not be reflected in the compiled graph."
            )
        if key not in self.nodes:
            raise ValueError(f"Need to add_node `{key}` first")
        self.entry_point = key

    def set_finish_point(self, key: str) -> None:
        return self.add_edge(key, END)

    def validate(self, interrupt: Optional[Sequence[str]] = None) -> None:
        all_starts = {src for src, _ in self.edges} | {src for src in self.branches}
        for node in self.nodes:
            if node not in all_starts:
                raise ValueError(f"Node `{node}` is a dead-end")

        if all(
            branch.ends is not None
            for branch_list in self.branches.values()
            for branch in branch_list
        ):
            all_ends = (
                {end for _, end in self.edges}
                | {
                    end
                    for branch_list in self.branches.values()
                    for branch in branch_list
                    for end in branch.ends.values()
                }
                | {self.entry_point}
            )

            for node in self.nodes:
                if node not in all_ends:
                    raise ValueError(f"Node `{node}` is not reachable")

        if interrupt:
            for node in interrupt:
                if node not in self.nodes:
                    raise ValueError(f"Node `{node}` is not present")

        self.compiled = True

    def compile(
        self,
        checkpointer: Optional[BaseCheckpointSaver] = None,
        interrupt_before: Optional[Sequence[str]] = None,
        interrupt_after: Optional[Sequence[str]] = None,
    ) -> "CompiledGraph":
        interrupt_before = interrupt_before or []
        interrupt_after = interrupt_after or []
        self.validate(interrupt=interrupt_before + interrupt_after)

        outgoing_edges = defaultdict(list)
        for start, end in self.edges:
            outgoing_edges[start].append(f"{end}:inbox" if end != END else END)

        nodes = {
            key: (Channel.subscribe_to(f"{key}:inbox") | node | Channel.write_to(key))
            for key, node in self.nodes.items()
        }

        for key in self.nodes:
            outgoing = outgoing_edges[key]
            edges_key = f"{key}:edges"
            if outgoing or key in self.branches:
                nodes[edges_key] = Channel.subscribe_to(key, tags=["langsmith:hidden"])
            if outgoing:
                nodes[edges_key] |= Channel.write_to(*[dest for dest in outgoing])
            if key in self.branches:
                for branch in self.branches[key]:
                    nodes[edges_key] |= RunnableLambda(
                        branch.runnable, name=f"{key}_condition"
                    )

        return CompiledGraph(
            graph=self,
            nodes=nodes,
            input=f"{self.entry_point}:inbox",
            output=END,
            hidden=[f"{node}:inbox" for node in self.nodes],
            checkpointer=checkpointer,
            interrupt=(
                [f"{node}:inbox" for node in interrupt_before]
                + [node for node in interrupt_after]
            ),
        )


class CompiledGraph(Pregel):
    graph: Graph

    def get_graph(self, config: Optional[RunnableConfig] = None) -> RunnableGraph:
        graph = RunnableGraph()
        graph.add_node(self.get_input_schema(config), "__start__")
        graph.add_node(self.get_output_schema(config), END)

        for key, node in self.graph.nodes.items():
            graph.add_node(node, key)
        for start, end in self.graph.edges:
            graph.add_edge(graph.nodes[start], graph.nodes[end])
        for start, branches in self.graph.branches.items():
            for i, branch in enumerate(branches):
                name = f"{start}_{branch.condition.__name__}"
                if i > 0:
                    name += f"_{i}"
                graph.add_node(
                    RunnableLambda(branch.runnable, name=branch.condition.__name__),
                    name,
                )
                graph.add_edge(graph.nodes[start], graph.nodes[name])
                for label, end in branch.ends.items():
                    graph.add_edge(graph.nodes[name], graph.nodes[end], label)
        graph.add_edge(graph.nodes["__start__"], graph.nodes[self.graph.entry_point])

        return graph
