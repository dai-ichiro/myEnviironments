import os

import cv2
import torch
from basicsr.utils import tensor2img
from pytorch_lightning import seed_everything
from torch import autocast

from ldm.inference_base import (diffusion_inference, get_adapters, get_base_argument_parser, get_sd_models)
from ldm.modules.extra_condition import api
from ldm.modules.extra_condition.api import (ExtraCondition, get_adapter_feature, get_cond_model)
from ldm.util import fix_cond_shapes

# for masactrl
from masactrl.masactrl_utils import regiter_attention_editor_ldm
from masactrl.masactrl import MutualSelfAttentionControl
from masactrl.masactrl import MutualSelfAttentionControlMask
from masactrl.masactrl import MutualSelfAttentionControlMaskAuto

torch.set_grad_enabled(False)


def main():
    supported_cond = [e.name for e in ExtraCondition]
    parser = get_base_argument_parser()
    parser.add_argument(
        '--which_cond',
        type=str,
        required=True,
        choices=supported_cond,
        help='which condition modality you want to test',
    )
    # [MasaCtrl added] reference cond path
    parser.add_argument(
        "--cond_path_src",
        type=str,
        default=None,
        help="the condition image path to synthesize the source image",
    )
    parser.add_argument(
        "--prompt_src",
        type=str,
        default=None,
        help="the prompt to synthesize the source image",
    )

    opt = parser.parse_args()
    which_cond = opt.which_cond
    if opt.outdir is None:
        opt.outdir = f'outputs/test-{which_cond}'
    os.makedirs(opt.outdir, exist_ok=True)
    if opt.resize_short_edge is None:
        print(f"you don't specify the resize_shot_edge, so the maximum resolution is set to {opt.max_resolution}")
    opt.device = torch.device("cuda") if torch.cuda.is_available() else torch.device("cpu")

    if os.path.isdir(opt.cond_path):  # for conditioning image folder
        image_paths = [os.path.join(opt.cond_path, f) for f in os.listdir(opt.cond_path)]
    else:
        image_paths = [opt.cond_path]
    print(image_paths)

    # prepare models
    sd_model, sampler = get_sd_models(opt)
    adapter = get_adapters(opt, getattr(ExtraCondition, which_cond))
    cond_model = None
    if opt.cond_inp_type == 'image':
        cond_model = get_cond_model(opt, getattr(ExtraCondition, which_cond))

    process_cond_module = getattr(api, f'get_cond_{which_cond}')

    # [MasaCtrl added] default STEP and LAYER params for MasaCtrl
    STEP = 4
    LAYER = 10

    if opt.cond_path_src:
        cond_src = process_cond_module(opt, opt.cond_path_src, opt.cond_inp_type, cond_model)
    if opt.cond_path_src:
        adapter_features_src, append_to_context_src = get_adapter_feature(cond_src, adapter)

    first_frame_filename = os.path.splitext(os.path.basename(opt.cond_path_src))[0]

    # inference
    with torch.inference_mode(), \
            sd_model.ema_scope(), \
            autocast('cuda'):
        for test_idx, cond_path in enumerate(image_paths):
            frame_filename = os.path.splitext(os.path.basename(cond_path))[0]
            seed_everything(opt.seed)
            for v_idx in range(opt.n_samples):
                editor = MutualSelfAttentionControl(STEP, LAYER)
                regiter_attention_editor_ldm(sd_model, editor)

                # seed_everything(opt.seed+v_idx+test_idx)
                
                cond = process_cond_module(opt, cond_path, opt.cond_inp_type, cond_model)

                '''
                base_count = len(os.listdir(opt.outdir)) // 2
                cv2.imwrite(os.path.join(opt.outdir, f'{base_count:05}_{which_cond}.png'), tensor2img(cond))
                if opt.cond_path_src:
                    cv2.imwrite(os.path.join(opt.outdir, f'{base_count:05}_{which_cond}_src.png'), tensor2img(cond_src))
                '''
                adapter_features, append_to_context = get_adapter_feature(cond, adapter)
                
                if opt.cond_path_src:
                    print("using reference guidance to synthesize image")
                    adapter_features = [torch.cat([adapter_features_src[i], adapter_features[i]]) for i in range(len(adapter_features))]
                else:
                    adapter_features = [torch.cat([torch.zeros_like(feats), feats]) for feats in adapter_features]

                if opt.scale > 1.:
                    adapter_features = [torch.cat([feats] * 2) for feats in adapter_features]

                # prepare the batch prompts
                if opt.prompt_src:
                    prompts = [opt.prompt_src, opt.prompt]
                else:
                    prompts = [opt.prompt] * 2

                # get text embedding
                c = sd_model.get_learned_conditioning(prompts)
                if opt.scale != 1.0:
                    uc = sd_model.get_learned_conditioning([""] * len(prompts))
                else:
                    uc = None
                c, uc = fix_cond_shapes(sd_model, c, uc)

                if not hasattr(opt, 'H'):
                    opt.H = 512
                    opt.W = 512
                shape = [opt.C, opt.H // opt.f, opt.W // opt.f]
                start_code = torch.randn([1, *shape], device=opt.device)
                start_code = start_code.expand(len(prompts), -1, -1, -1)

                samples_latents, _ = sampler.sample(
                    S=opt.steps,
                    conditioning=c,
                    batch_size=len(prompts),
                    shape=shape,
                    verbose=False,
                    unconditional_guidance_scale=opt.scale,
                    unconditional_conditioning=uc,
                    x_T=start_code,
                    features_adapter=adapter_features,
                    append_to_context=append_to_context,
                    cond_tau=opt.cond_tau,
                    style_cond_tau=opt.style_cond_tau,
                )

                x_samples = sd_model.decode_first_stage(samples_latents)
                x_samples = torch.clamp((x_samples + 1.0) / 2.0, min=0.0, max=1.0)

                #cv2.imwrite(os.path.join(opt.outdir, f'{base_count:05}_all_result.png'), tensor2img(x_samples))
                # save the prompts and seed
                with open(os.path.join(opt.outdir, "log.txt"), "w") as f:
                    for prom in prompts:
                        f.write(prom)
                        f.write("\n")
                    f.write(f"seed: {opt.seed}")
                
                # first frame
                if test_idx == 0:
                    cv2.imwrite(os.path.join(opt.outdir, f'{first_frame_filename}_result.png'), tensor2img(x_samples[0]))
                
                cv2.imwrite(os.path.join(opt.outdir, f'{frame_filename}_result.png'), tensor2img(x_samples[1]))

if __name__ == '__main__':
    main()
