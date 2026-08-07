from manimlib import *

class TestPipeline(Scene):
    def construct(self):
        title = Text("Axon × Manim", font_size=60)
        subtitle = Text("pipeline check", font_size=30, color=GREY)
        subtitle.next_to(title, DOWN)

        circle = Circle(color=BLUE, fill_opacity=0.3)
        circle.shift(DOWN * 1.5)

        self.play(Write(title), run_time=1)
        self.play(FadeIn(subtitle), run_time=0.5)
        self.play(GrowFromCenter(circle), run_time=1)
        self.wait(0.5)
