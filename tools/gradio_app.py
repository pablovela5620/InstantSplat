from instant_splat.gradio_ui.multi_img_ui import multi_img_block
import gradio as gr

title = """# InstantSplat: Unofficial Demo of Sparse-view SfM-free Gaussian Splatting in Seconds"""
description1 = """
    <a title="Website" href="https://instantsplat.github.io/" target="_blank" rel="noopener noreferrer" style="display: inline-block;">
        <img src="https://www.obukhov.ai/img/badges/badge-website.svg">
    </a>
    <a title="arXiv" href="https://arxiv.org/abs/2403.20309" target="_blank" rel="noopener noreferrer" style="display: inline-block;">
        <img src="https://www.obukhov.ai/img/badges/badge-pdf.svg">
    </a>
    <a title="Github" href="https://github.com/pablovela5620/InstantSplat" target="_blank" rel="noopener noreferrer" style="display: inline-block;">
        <img src="https://img.shields.io/github/stars/pablovela5620/InstantSplat?label=GitHub%20%E2%98%85&logo=github&color=C8C" alt="badge-github-stars">
    </a>
    <a title="Social" href="https://x.com/pablovelagomez1" target="_blank" rel="noopener noreferrer" style="display: inline-block;">
        <img src="https://www.obukhov.ai/img/badges/badge-social.svg" alt="social">
    </a>
"""
description2 = "Using Rerun to visualize the results of InstantSplat"

with gr.Blocks() as demo:
    gr.Markdown(title)
    gr.Markdown(description1)
    gr.Markdown(description2)
    with gr.Tab(label="Multi Image"):
        multi_img_block.render()

if __name__ == "__main__":
    demo.launch(server_name="0.0.0.0")
