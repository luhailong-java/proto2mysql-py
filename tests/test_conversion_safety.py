"""protobuf 转换辅助层的拒错与资源上界回归测试。"""

from __future__ import annotations

from types import ModuleType

import pytest
from google.protobuf import descriptor_pb2, descriptor_pool, message_factory

from proto2mysql import pbconv
from proto2mysql._wire import scan_fields
from proto2mysql.registry import iter_file_descriptors


def test_repeated_detection_supports_old_and_new_descriptor_apis():
    """最低 protobuf 5.x 只有 label，新版优先使用 is_repeated。"""

    class LegacyRepeated:
        label = descriptor_pb2.FieldDescriptorProto.LABEL_REPEATED

    class LegacyOptional:
        label = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL

    class ModernRepeated:
        is_repeated = True
        label = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL

    assert pbconv.is_repeated_field(LegacyRepeated()) is True
    assert pbconv.is_repeated_field(LegacyOptional()) is False
    assert pbconv.is_repeated_field(ModernRepeated()) is True


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        (b"\x09" + b"\x00" * 7, "truncated fixed64"),
        (b"\x0d" + b"\x00" * 3, "truncated fixed32"),
        (b"\x0b\x10\x01", "unclosed group"),
    ],
)
def test_wire_scanner_rejects_truncated_fixed_values_and_open_group(payload, error):
    with pytest.raises(ValueError, match=error):
        scan_fields(payload)


def test_registry_scan_does_not_trigger_module_getattr():
    module = ModuleType("lazy_non_proto_module")
    calls: list[str] = []

    def lazy_getattr(name: str):
        calls.append(name)
        raise RuntimeError("optional dependency is unavailable")

    module.__getattr__ = lazy_getattr

    assert list(iter_file_descriptors([module])) == []
    assert calls == []


def test_dynamic_descriptor_encoder_caches_have_a_hard_limit():
    """大量动态描述符可被处理，但不能永久钉住每个临时 descriptor pool。"""
    file_proto = descriptor_pb2.FileDescriptorProto(
        name="encoder_cache_bound.proto",
        package="cache_bound",
        syntax="proto3",
    )
    message_proto = file_proto.message_type.add(name="wide_message")
    for number in range(1, pbconv._ENCODER_CACHE_SIZE + 65):
        message_proto.field.add(
            name=f"field_{number}",
            number=number,
            label=descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL,
            type=descriptor_pb2.FieldDescriptorProto.TYPE_INT32,
        )

    pool = descriptor_pool.DescriptorPool()
    file_descriptor = pool.Add(file_proto)
    descriptor = file_descriptor.message_types_by_name["wide_message"]
    message = message_factory.GetMessageClass(descriptor)()
    for field_descriptor in descriptor.fields:
        assert pbconv.serialize_field_value(message, field_descriptor) == "0"

    assert pbconv.text_encoder.cache_info().currsize <= pbconv._ENCODER_CACHE_SIZE
    assert pbconv.value_encoder.cache_info().currsize <= pbconv._ENCODER_CACHE_SIZE
