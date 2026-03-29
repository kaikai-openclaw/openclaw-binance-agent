"""
Skill 基类模块

所有 Skill 继承 BaseSkill，内置 JSON Schema 校验（draft-07）和执行日志记录。
标准执行流程：加载输入 → Schema 校验输入 → 执行业务逻辑 → Schema 校验输出 → 存储状态。

需求: 6.6, 9.2, 9.3, 9.4, 9.5, 9.6
"""

import logging
import time

from jsonschema import Draft7Validator, FormatChecker, ValidationError

from src.infra.state_store import StateStore

log = logging.getLogger(__name__)


class SchemaValidationError(Exception):
    """
    JSON Schema 校验失败时抛出的异常。

    包含校验错误详情，用于区分输入校验失败和输出校验失败。
    """

    def __init__(self, message: str, errors: list[str] | None = None) -> None:
        super().__init__(message)
        self.errors = errors or []


class BaseSkill:
    """
    Skill 基类，提供标准化的执行流程。

    子类需要：
    1. 设置 self.name 属性（Skill 名称）
    2. 实现 run(input_data) 方法（具体业务逻辑）
    """

    def __init__(
        self,
        state_store: StateStore,
        input_schema: dict,
        output_schema: dict,
    ) -> None:
        """
        初始化 BaseSkill。

        参数:
            state_store: 状态存储实例，用于读写 Skill 数据
            input_schema: 输入数据的 JSON Schema（draft-07）
            output_schema: 输出数据的 JSON Schema（draft-07）
        """
        self.state_store = state_store
        self.input_schema = input_schema
        self.output_schema = output_schema
        # 预编译校验器，提升校验性能
        self._input_validator = Draft7Validator(
            input_schema, format_checker=FormatChecker()
        )
        self._output_validator = Draft7Validator(
            output_schema, format_checker=FormatChecker()
        )
        # 子类应覆盖此属性
        self.name: str = self.__class__.__name__

    def execute(self, input_state_id: str | None = None) -> str:
        """
        执行 Skill 的标准流程：
        1. 从 State_Store 加载输入数据（若有 input_state_id）
        2. 使用 input_schema 校验输入
        3. 调用 run() 执行业务逻辑
        4. 使用 output_schema 校验输出
        5. 将输出存入 State_Store，返回新的 state_id

        参数:
            input_state_id: 上游 Skill 输出的状态 ID，为 None 时输入为空字典

        返回:
            新生成的 state_id（UUID v4）

        异常:
            SchemaValidationError: 输入或输出数据未通过 Schema 校验
        """
        start_time = time.monotonic()
        log.info(
            f"[{self.name}] 开始执行, input_state_id={input_state_id}"
        )

        try:
            # 步骤 1：加载输入数据
            if input_state_id is not None:
                input_data = self.state_store.load(input_state_id)
            else:
                input_data = {}

            # 步骤 2：校验输入数据
            self._validate_input(input_data)

            # 步骤 3：执行业务逻辑
            output_data = self.run(input_data)

            # 步骤 4：校验输出数据
            self._validate_output(output_data)

            # 步骤 5：存储输出并返回 state_id
            state_id = self.state_store.save(self.name, output_data)

            elapsed = time.monotonic() - start_time
            log.info(
                f"[{self.name}] 执行完成, "
                f"output_state_id={state_id}, "
                f"耗时={elapsed:.3f}s, 状态=success"
            )
            return state_id

        except SchemaValidationError:
            elapsed = time.monotonic() - start_time
            log.error(
                f"[{self.name}] 执行失败, "
                f"耗时={elapsed:.3f}s, 状态=failed, "
                f"原因=Schema校验失败"
            )
            raise

        except Exception as exc:
            elapsed = time.monotonic() - start_time
            log.error(
                f"[{self.name}] 执行失败, "
                f"耗时={elapsed:.3f}s, 状态=failed, "
                f"原因={exc}"
            )
            raise

    def run(self, input_data: dict) -> dict:
        """
        子类实现具体业务逻辑。

        参数:
            input_data: 经过 Schema 校验的输入数据

        返回:
            输出数据字典（将被 output_schema 校验）
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} 必须实现 run() 方法"
        )

    def _validate_input(self, data: dict) -> None:
        """
        使用 input_schema 校验输入数据。

        参数:
            data: 待校验的输入数据

        异常:
            SchemaValidationError: 校验失败时抛出，包含错误详情
        """
        errors = list(self._input_validator.iter_errors(data))
        if errors:
            error_messages = [e.message for e in errors]
            raise SchemaValidationError(
                f"[{self.name}] 输入数据 Schema 校验失败: {error_messages}",
                errors=error_messages,
            )

    def _validate_output(self, data: dict) -> None:
        """
        使用 output_schema 校验输出数据。

        参数:
            data: 待校验的输出数据

        异常:
            SchemaValidationError: 校验失败时抛出，包含错误详情
        """
        errors = list(self._output_validator.iter_errors(data))
        if errors:
            error_messages = [e.message for e in errors]
            raise SchemaValidationError(
                f"[{self.name}] 输出数据 Schema 校验失败: {error_messages}",
                errors=error_messages,
            )
