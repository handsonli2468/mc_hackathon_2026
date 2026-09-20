"""Exercise allocation, kernels, GEMM, convolution and attention with sync."""
import json
import os
import torch
import torch.nn.functional as F


def main():
    print('IMPORT', torch.__version__, 'HIP', torch.version.hip, flush=True)
    if not torch.version.hip or not torch.cuda.is_available():
        raise RuntimeError('AMD HIP build and an accessible GPU are required')
    expected = os.getenv('EXPECTED_HIP_MAJOR', '')
    if expected and torch.version.hip.split('.')[0] != expected:
        raise RuntimeError(f'Expected HIP {expected}, got {torch.version.hip}')
    props = torch.cuda.get_device_properties(0)
    arch = getattr(props, 'gcnArchName', '').split(':')[0]
    compiled = torch.cuda.get_arch_list()
    print('DEVICE', props.name, 'ARCH', arch, 'COMPILED', compiled, flush=True)
    expected_arch = os.getenv('EXPECTED_GPU_ARCH', 'gfx1152')
    if arch and arch != expected_arch:
        raise RuntimeError(f'Expected {expected_arch}; detected {arch}')
    # Modern split wheels may not enumerate all dynamically loaded code objects.
    # Record the list, but validate actual kernels rather than infer from the list alone.
    with torch.inference_mode():
        x = torch.empty((64, 64), device='cuda')
        torch.cuda.synchronize()
        print('ALLOCATION_OK', flush=True)
        x.fill_(1)
        torch.cuda.synchronize()
        print('FILL_OK', flush=True)
        for dtype in (torch.float32, torch.float16):
            a = x.to(dtype)
            y = a @ a
            torch.cuda.synchronize()
            if not torch.allclose(y.float(), torch.full_like(y.float(), 64)):
                raise RuntimeError('GEMM result mismatch')
            print('GEMM_OK', str(dtype), flush=True)
        with torch.autocast('cuda', dtype=torch.float16):
            image = torch.ones((1, 3, 32, 32), device='cuda')
            kernel = torch.ones((8, 3, 3, 3), device='cuda')
            y = F.conv2d(image, kernel)
            torch.cuda.synchronize()
            if not torch.isfinite(y).all().item():
                raise RuntimeError('Convolution produced nonfinite values')
            print('CONV_OK', flush=True)
            q = torch.ones((1, 2, 16, 32), device='cuda')
            y = F.scaled_dot_product_attention(q, q, q)
            torch.cuda.synchronize()
            if not torch.isfinite(y).all().item():
                raise RuntimeError('Attention produced nonfinite values')
            print('ATTENTION_OK', flush=True)
    print('GPU_PASS ' + json.dumps(dict(device=props.name, arch=arch, compiled=compiled,
                                        torch=torch.__version__, hip=torch.version.hip)), flush=True)


if __name__ == '__main__':
    main()
