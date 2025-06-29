import contextlib
import re
import threading
from dataclasses import dataclass
from functools import partial
from typing import Dict, List, Union

import torch

from mttl.logging import logger
from mttl.models.base_model import BaseExpertModel
from mttl.models.containers.base import (
    ContainerFullException,
    ExpertContainer,
    MergeableContainer,
    containers_iterator,
)
from mttl.models.containers.selectors.base import (
    AutoSelectorConfig,
    LoadableSelectorConfig,
    MultiSelectorConfig,
    Selector,
    SelectorConfig,
    SelectorsCache,
)
from mttl.models.expert_config import BaseExpertModelConfig
from mttl.models.library.expert import Expert, ExpertInfo
from mttl.models.library.expert_library import ExpertLibrary
from mttl.models.modifiers.base import (
    AutoModifierConfig,
    MergeableModifierMixin,
    Modifier,
)
from mttl.models.modifiers.modify_model import modify_transformer


@contextlib.contextmanager
def disable_adapters(model):
    """Context manager to disable all adapters in a model.

    Args:
        model (BaseExpertModel): The model to disable adapters in.
    """
    model.disable_adapters()

    yield

    model.enable_adapters()


@dataclass
class ExpertModelConfig(BaseExpertModelConfig):
    task_name: str = None
    expert_name: str = None
    modifier_config: AutoModifierConfig = None


@BaseExpertModel.register("expert_model", config_cls=ExpertModelConfig)
class ExpertModel(BaseExpertModel):
    def __init__(
        self,
        config: ExpertModelConfig,
        model_object: torch.nn.Module = None,
        **loading_kwargs,
    ):
        super().__init__(config, model_object=model_object, **loading_kwargs)

        if config.modifier_config is not None:
            modify_transformer(self.model, config.modifier_config)

    def enable_adapters(self):
        for module in self.modifiers:
            module.enable()

    def disable_adapters(self):
        for module in self.modifiers:
            module.disable()

    @property
    def modifiers(self):
        modifiers = []
        for name, module in self.model.named_modules():
            if isinstance(module, Modifier):
                modifiers.append(module)
        return modifiers

    def merge_and_save_base_model(self, output_dir, device="cpu"):
        """
        Merge loaded adapters and save the base model in the huggingface format
        to the output directory. Moves the model to the specified device before merging.
        """
        import copy

        from transformers import AutoTokenizer

        # move model to cpu to avoid memory issues
        self.model.to(device)

        merged = []
        model_copy = copy.deepcopy(self.model)

        for name, module in model_copy.named_modules():
            for c_name, child in module.named_children():
                if isinstance(child, Modifier):
                    if isinstance(child, MergeableModifierMixin):
                        # merge the adapter with the layer
                        child.merge_with_layer()
                        # remove the modifier and set the layer
                        setattr(
                            module,
                            c_name,
                            child.layer,
                        )
                        merged.append(name)
                    else:
                        raise ValueError(
                            "Modifier {} is not mergeable!".format(child.__class__)
                        )

        logger.info("Merged layers: %s" % ", ".join(merged))
        logger.info("Saving merged model to: %s" % output_dir)

        model_copy.save_pretrained(output_dir)
        tokenizer = AutoTokenizer.from_pretrained(
            self.config.base_model
        ).save_pretrained(output_dir)

    @classmethod
    def from_pretrained_peft(cls, path_in_repo: str, **kwargs):
        """Instantiate an expert model from a pretrained PEFT adapter."""
        from mttl.models.library.peft import load_expert_from_peft_checkpoint

        expert = load_expert_from_peft_checkpoint(path_in_repo)
        config = ExpertModelConfig(
            base_model=expert.expert_info.expert_model,
            expert_name=expert.expert_info.expert_name,
            modifier_config=expert.expert_info.expert_config,
        )
        self = cls(config, **kwargs)
        self.model.load_state_dict(expert.expert_weights, strict=False)
        return self

    def as_expert(self, training_config=None):
        state_dict = self.state_dict()
        self._delete_non_trainable_params(state_dict)

        # to use as an expert, we need to remove a `model.` prefix
        state_dict = {k[len("model.") :]: v for k, v in state_dict.items()}

        # inject expert info in the expert checkpoint
        expert_info = ExpertInfo(
            expert_name=self.config.expert_name,
            expert_task_name=self.config.task_name,
            expert_model=self.config.base_model,
            expert_config=self.config.modifier_config,
            training_config=training_config,
        )
        return Expert(
            expert_info=expert_info,
            expert_weights=state_dict,
        )


class MultiExpertMixin:
    """Encapsulates all methods related to multi-expert models."""

    @property
    def selector_cache(self) -> SelectorsCache:
        if not hasattr(self, "_selector_cache"):
            self._selector_cache = SelectorsCache()
        return self._selector_cache

    @property
    def experts_infos(self) -> Dict[str, ExpertInfo]:
        if not hasattr(self, "_experts_infos"):
            self._experts_infos = {}
        return self._experts_infos

    @property
    def selector_config(self) -> AutoSelectorConfig:
        if not hasattr(self, "_selector_config"):
            self._selector_config = self.config.selector_config
        return self._selector_config

    @selector_config.setter
    def selector_config(self, value: AutoSelectorConfig):
        self._selector_config = value

    def enable_adapters(self):
        for module in self.experts_containers:
            module.enable()

    def disable_adapters(self):
        for module in self.experts_containers:
            module.disable()

    @property
    def experts_names(self):
        return list(self.experts_infos.keys())

    def unset_default_expert(self):
        """Propagate default expert to all containers that contain it."""
        for container in self.experts_containers:
            container.default_expert_name = None

    def set_default_expert(self, expert_name):
        """Propagate default expert to all containers that contain it."""
        if expert_name not in self.experts_infos:
            raise ValueError(f"Expert {expert_name} not found in the model.")

        for container in self.experts_containers:
            if expert_name in container.expert_infos:
                container.default_expert_name = expert_name
            else:
                raise ValueError(f"Expert {expert_name} not found in the container.")

    @property
    def lock(self):
        if not hasattr(self, "_lock"):
            self._lock = threading.Lock()
        return self._lock

    @property
    def experts_containers(self) -> List[ExpertContainer]:
        containers = []
        for _, module in self.model.named_modules():
            for _, child in dict(module.named_children()).items():
                if isinstance(child, ExpertContainer):
                    containers.append(child)
        return containers

    @property
    def selectors(self) -> Dict[str, List[Selector]]:
        return {
            key: list(self.selector_cache.get(key).values())
            for key in self.selector_cache.keys()
        }

    def delete_expert_container(self):
        """
        Replaces the expert container with the expert with the given name.
        """
        for _, module in self.model.named_modules():
            for c_name, child in dict(module.named_children()).items():
                if isinstance(child, ExpertContainer):
                    setattr(module, c_name, child.layer)

        self.selector_cache.clear()
        self.experts_infos.clear()

    def add_experts_from_library(self, library):
        import concurrent.futures

        from tqdm.auto import tqdm

        if type(library) == str:
            from mttl.models.library.expert_library import ExpertLibrary

            library = ExpertLibrary.get_expert_library(library)

        def add_module(self, module_name):
            expert_dump = library[module_name]
            self.add_expert_instance(expert_dump)

        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as executor:
            # Create a list to hold the futures
            futures = []
            for element in library.keys():
                futures.append(executor.submit(partial(add_module, self), element))

            # Progress bar setup
            with tqdm(
                total=len(library), desc="Adding experts...", unit="expert"
            ) as progress_bar:
                for result in concurrent.futures.as_completed(futures):
                    # raise exception
                    if result.exception():
                        raise result.exception()
                    progress_bar.update(1)

    def add_experts_from_dict(self, experts_dict, action="route"):
        for expert_name, expert_dump in experts_dict.items():
            self.add_expert_instance(expert_dump, expert_name, action=action)

    def add_empty_expert(
        self,
        expert_name,
        expert_config=None,
        is_default=False,
    ) -> Expert:
        """Adds a new empty expert to the model."""
        new_expert = Expert(
            expert_info=ExpertInfo(
                expert_name,
                expert_config=expert_config,
                expert_model=self.config.base_model,
            ),
        )

        new_expert = self.add_expert_instance(new_expert, is_default=is_default)
        logger.info("Added empty expert: {}".format(expert_name))
        return new_expert

    def add_expert(
        self,
        expert_path: str,
        expert_name: str = None,
        action: str = "route",
        is_default: bool = False,
    ):
        return self.load_expert(
            expert_path,
            expert_name=expert_name,
            action=action,
            is_default=is_default,
        )

    def load_expert(
        self,
        expert_path: str,
        expert_name: str = None,
        action: str = "route",
        is_default: bool = False,
    ):
        from mttl.models.library.expert import Expert, load_expert

        expert: Expert = load_expert(
            expert_path,
            expert_name=expert_name,
        )

        if self.hparams.model != expert.expert_info.expert_model:
            raise ValueError(
                "The expert has been trained on top of a different model!"
                " Detected: {} - Expected: {}".format(
                    expert.expert_info.expert_model, self.hparams.model
                )
            )

        logger.info(
            f"Adding expert with name {expert.name}... with action ... {action}!"
        )
        self.add_expert_instance(expert, action=action, is_default=is_default)

    def _get_selector_config(self, model_modifier: str) -> SelectorConfig:
        if not self.selector_config:
            return None
        if isinstance(self.selector_config, MultiSelectorConfig):
            return self.selector_config.get(model_modifier)
        else:
            return self.selector_config

    def add_expert_instance(
        self,
        expert_instance: Expert,
        expert_name=None,
        action="route",
        is_default=False,
    ) -> Expert:
        """
        If action is merge, then the expert is merged with the existing model, and we return None.
        """
        from mttl.models.containers import get_default_container_class

        if expert_name is not None:
            # we want to load expert instance with a given name (might be different from the one in the expert instance)
            # we dont want to change expert instance though!
            # will create a copy for now (maybe safer), alternatively can change the name and set it back at the end of the function
            expert_instance = expert_instance.clone()
            expert_instance.name = expert_name

        with self.lock:
            modifier_name = expert_instance.expert_config.modifier_name
            selector_config = self._get_selector_config(modifier_name)

            get_default_container_class(modifier_name).modify_transformer(
                self.model,
                expert_instance,
                action=action,
                is_default=is_default,
                selector_config=selector_config,
                selector_cache=self.selector_cache,
            )

            if action != "merge":
                self.experts_infos[expert_instance.name] = expert_instance.expert_info
                # reload the expert instance to fill the weights properly if this was an empty expert
                expert_instance = self.get_expert_instance(expert_instance.name)
                return expert_instance

    def set_selector(
        self,
        modifier_name: str,
        selector_config: SelectorConfig,
    ):
        from mttl.models.containers import replace_selector_for_container

        n_selectors, n_selectors_views = replace_selector_for_container(
            self.model,
            modifier_name,
            selector_config,
            self.selector_cache,
            force_replace=True,
        )

        # refresh current selector config
        self.selector_config = MultiSelectorConfig()

        for modifier_name, selectors in self.selectors.items():
            if len(selectors) == 0:
                continue

            selector_config = selectors[0].config
            self.selector_config[modifier_name] = selector_config

        logger.info(
            "Created {} selectors and {} views.".format(n_selectors, n_selectors_views)
        )

    def extract_parameters(self, p_name_pattern=".*lora.*"):
        """
        Extracts task embeddings for parameters matching the given pattern.

        Args:
            p_name_pattern (str, optional): Regular expression pattern to match parameter names.
                Defaults to ".*lora.*".

        Returns:
            torch.Tensor: Concatenated tensor of task embeddings for the matched parameters.
        """
        para_list = []
        for name, param in self.model.named_parameters():
            if re.fullmatch(p_name_pattern, name):
                para_list.append(param.reshape(-1))
        return torch.cat(para_list)

    def get_expert_instance(self, expert_name):
        """
        Retrieves an instance of the specified expert from the model.

        Args:
            expert_name (str): The name of the expert to retrieve.
            silent (bool, optional): If True, suppresses the ValueError exception when the expert is not found.
                Defaults to True.

        Returns:
            expert: An instance of the specified expert.

        Raises:
            AssertionError: If the expert name is not found in the model, if no expert containers are found,
                or if the expert names are not unique.
            ValueError: If the expert is not found in the model and silent is False.
        """
        assert (
            expert_name in self.experts_names
        ), f"Expert {expert_name} not found in the model."
        assert (
            len(self.experts_containers) > 0
        ), "No expert containers found in the model."
        assert len(set(self.experts_names)) == len(
            self.experts_names
        ), "Expert names are not unique."

        expert_params = {}
        for container in self.experts_containers:
            retrieved_expert = None
            if expert_name in container.expert_infos:
                expert_info = container.expert_infos[expert_name]
                expert_weights = container[expert_name].state_dict()
                expert_weights = {
                    f"{container.layer_name}.{k}": v for k, v in expert_weights.items()
                }
                expert_params.update(expert_weights)

                retrieved_expert = Expert(
                    expert_info=expert_info, expert_weights=expert_params
                )
        return retrieved_expert

    def save_to_library(self, library_id):
        """
        Saves the current loaded experts to the specified library.

        Args:
            library_id (str): The ID of the library to save the experts to.
        """
        library = ExpertLibrary.get_expert_library(library_id, create=True)
        for expert_name in self.experts_names:
            expert = self.get_expert_instance(expert_name)
            library.add_expert(expert)
        return library

    def clear_experts(self):
        """Removes all experts and containers from the huggingface model."""
        from mttl.models.containers.base import clear_containers

        clear_containers(self.model)

        self.experts_infos.clear()
        self.selector_cache.clear()
        self.selector_config = None


@dataclass
class MultiExpertModelConfig(BaseExpertModelConfig):
    # if default_expert_name is not None, then we set it as the default expert
    default_expert_name: str = None
    # if expert_infos is not None, then we load experts from the expert_infos
    expert_infos: List[ExpertInfo] = None
    # if selector_config is not None, then we use it to select experts during inference
    selector_config: AutoSelectorConfig = None

    def __post_init__(self):
        # error out if neither library_id nor expert_infos is set and default_expert_name is set
        if self.expert_infos is None and self.default_expert_name is not None:
            raise ValueError(
                "Cannot set `default_expert_name` if neither `library_id` nor `expert_infos` is set."
            )


@BaseExpertModel.register("multi_expert_model", config_cls=MultiExpertModelConfig)
class MultiExpertModel(BaseExpertModel, MultiExpertMixin):
    """Adds all functions and properties for a multi-expert model."""

    @classmethod
    def init_from_model(cls, config, model: torch.nn.Module) -> "MultiExpertModel":
        """Initialize a multi-expert model from an existing model."""
        config.base_model = None

        return MultiExpertModel(config, model_object=model)

    @classmethod
    def from_pretrained_peft(
        cls, path_in_repo: str, set_as_default: bool = True, **kwargs
    ):
        """Instantiate an expert model from a pretrained PEFT adapter."""
        from mttl.models.library.peft import load_expert_from_peft_checkpoint

        expert = load_expert_from_peft_checkpoint(path_in_repo)
        config = MultiExpertModelConfig(
            base_model=expert.expert_info.expert_model,
        )
        self = cls(config, **kwargs)
        self.add_expert_instance(expert, is_default=set_as_default)
        return self

    @classmethod
    def from_pretrained_library(
        cls,
        library_id: Union[str, ExpertLibrary],
        selector_config: Union[SelectorConfig, Dict[str, SelectorConfig]] = None,
        remote_token: str = None,
        default_expert_name: str = None,
        **loading_kwargs,
    ):
        if not isinstance(library_id, ExpertLibrary):
            library = ExpertLibrary.get_expert_library(
                repo_id=library_id,
                token=remote_token,
            )
            repo_id = library_id
        else:
            library = library_id
            repo_id = library_id.uri

        # get a config file from the library, and initialize the expert model
        an_expert = library[next(iter(library.keys()))]

        # set selector for the added experts
        if selector_config is not None:
            if isinstance(selector_config, LoadableSelectorConfig):
                selector_config.library_id = repo_id

            elif isinstance(selector_config, MultiSelectorConfig):
                for modifier_name, cfg in selector_config.items():
                    # inject the library id if it is None
                    if (
                        isinstance(cfg, LoadableSelectorConfig)
                        and cfg.library_id is None
                    ):
                        cfg.library_id = repo_id
        else:
            logger.info("No selector config provided, assuming expert name selector!")

        config = MultiExpertModelConfig(
            an_expert.expert_info.model,
            default_expert_name=default_expert_name,
            selector_config=selector_config,
        )
        model = cls(config, **loading_kwargs)
        model.add_experts_from_library(library)
        return model

    def _clear_library_id_from_selector_config(self) -> SelectorConfig:
        """When saving the model, we must clear the library id given that the weights are saved in the checkpoint."""
        import copy

        selector_config = copy.deepcopy(self.selector_config)

        if selector_config is not None:
            if isinstance(selector_config, MultiSelectorConfig):
                for key, config in selector_config.items():

                    if isinstance(config, LoadableSelectorConfig):
                        config.library_id = None

            elif isinstance(selector_config, LoadableSelectorConfig):
                selector_config.library_id = None

        return selector_config

    def task_vector_apply(self, task_merged_vectors):
        """
        Apply the task merged vectors to the model
        """
        # merge the task vectors to the model
        for name, param in self.model.named_parameters():
            name = name.split(".weight")[0]
            if name in task_merged_vectors.keys():
                logger.info(f"Merging {name} to the model")
                ## some times the shape is the reverse the task_merged_vectors
                if param.shape != task_merged_vectors[name].shape:
                    print(
                        f"shape mismatch {param.shape} {task_merged_vectors[name].shape}"
                    )
                    task_merged_vectors[name] = task_merged_vectors[name].T
                res = param + task_merged_vectors[name]
                param.data.copy_(res)

    def merge_and_save_base_model(self, output_dir, expert_name, device="cpu"):
        """
        Merge the specific expert and save the base model in the huggingface format
        to the output directory. Moves the model to the specified device before merging.
        """
        import copy

        from transformers import AutoTokenizer

        from mttl.models.containers.base import clear_containers

        if expert_name not in self.experts_infos:
            raise ValueError(f"Expert {expert_name} not found in the model.")

        # move model to cpu to avoid memory issues
        self.model.to(device)

        merged = []
        model_copy = copy.deepcopy(self.model)

        # get the modifier type of the expert name
        modifier_name = self.experts_infos[expert_name].expert_config.modifier_name

        for container in containers_iterator(model_copy):
            if expert_name in container.expert_infos:
                if not isinstance(container, MergeableContainer):
                    raise ValueError(
                        "Cannot merge expert loaded in a non-mergeable container. Either change container type or expert type."
                    )

                container.merge_expert(expert_name)
                merged.append(container.layer_name)

        # now clear all containers and save the model
        clear_containers(model_copy)

        logger.info("Merged layers: %s" % ", ".join(merged))
        logger.info("Saving merged model to: %s" % output_dir)

        model_copy.save_pretrained(output_dir)
        tokenizer = AutoTokenizer.from_pretrained(
            self.config.base_model
        ).save_pretrained(output_dir)

    def save_pretrained(self, save_directory, **kwargs):
        # need to make sure that config is in sync with the model before saving
        self.config.expert_infos = list(self.experts_infos.values())
        # if we save the model, and we have a library_id in the selector config, we need to clear it
        # because the weights will be save in the checkpoint
        self.config.selector_config = self._clear_library_id_from_selector_config()
        super().save_pretrained(save_directory, **kwargs)

    def __init__(self, config, **loading_kwargs):
        super().__init__(config, **loading_kwargs)

        if self.config.expert_infos is not None:
            for expert_info in self.config.expert_infos:
                expert_info: ExpertInfo
                self.add_empty_expert(
                    expert_info.expert_name,
                    expert_info.expert_config,
                )

            if self.config.default_expert_name:
                self.set_default_expert(self.config.default_expert_name)


@dataclass
class MoEModelConfig(BaseExpertModelConfig):
    # if library_id is not None, then we load experts from the library
    library_id: str = None
    # how many experts to add if not in library
    moe_num_experts: int = 1
    # if selector_config is not None, then we use it to select experts
    selector_config: AutoSelectorConfig = None
    # if modifier_config is not None, then we create moe_num_experts with this modifier
    modifier_config: AutoModifierConfig = None


@BaseExpertModel.register("moe_model", config_cls=MoEModelConfig)
class MoEModel(BaseExpertModel, MultiExpertMixin):
    def __init__(self, config, **kwargs):
        super().__init__(config, **kwargs)

        self.modifier_config = config.modifier_config

        if not self.config.library_id and self.config.moe_num_experts >= 1:
            self.add_empty_experts()
            self.moe_num_experts = self.config.moe_num_experts
        else:
            expert_library = ExpertLibrary.get_expert_library(self.config.library_id)
            assert len(expert_library) > 0, "No experts found in the library."
            for i, expert in enumerate(sorted(list(expert_library.keys()))):
                self.add_expert_instance(expert_library[expert], expert_name=expert)

            self.moe_num_experts = i + 1

    def add_empty_experts(self):
        for i in range(self.config.moe_num_experts):
            try:
                self.add_empty_expert(f"e{i}", self.modifier_config)
            except ContainerFullException:
                logger.info(
                    f"Added all {self.config.moe_num_experts} experts to container."
                )
                break
