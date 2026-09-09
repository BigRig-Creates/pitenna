#!/usr/bin/env python3
"""
Separate process to output content to HDMI1 display
This runs as a separate process to handle HDMI1 output
"""

import sys
import os
import pygame
from PIL import Image

HDMI1_SIZE = (1024, 768)

def main():
    # Write to stderr so it shows in terminal
    sys.stderr.write("HDMI1 process starting...\n")
    sys.stderr.flush()
    
    # Set up for HDMI1 - Raspberry Pi OS Lite compatible
    # Remove X11 environment if present
    if 'DISPLAY' in os.environ:
        del os.environ['DISPLAY']
    
    # Use KMS/DRM driver (works without X server)
    os.environ['SDL_VIDEODRIVER'] = 'kmsdrm'
    
    try:
        pygame.init()
        sys.stderr.write("Pygame initialized\n")
        sys.stderr.flush()
    except Exception as e:
        sys.stderr.write(f"Pygame init failed: {e}\n")
        sys.stderr.flush()
        sys.exit(1)
    
    # Try to create display for HDMI1
    try:
        # Check available displays
        num_displays = pygame.display.get_num_displays()
        sys.stderr.write(f"HDMI1 process: Found {num_displays} display(s)\n")
        sys.stderr.flush()
        
        if num_displays > 1:
            # Try to use display 1
            screen = pygame.display.set_mode(HDMI1_SIZE, pygame.FULLSCREEN, display=1)
            sys.stderr.write("HDMI1 display initialized on display 1\n")
            sys.stderr.flush()
        else:
            # Fallback - try display 0 but on different output
            sys.stderr.write("Only 1 display found, trying display 0\n")
            sys.stderr.flush()
            screen = pygame.display.set_mode(HDMI1_SIZE, pygame.FULLSCREEN, display=0)
            sys.stderr.write("HDMI1 display initialized on display 0 (may conflict with HDMI0)\n")
            sys.stderr.flush()
        
        pygame.mouse.set_visible(False)
    except Exception as e:
        sys.stderr.write(f"Failed to initialize HDMI1 display: {e}\n")
        import traceback
        traceback.print_exc(file=sys.stderr)
        sys.stderr.flush()
        # Don't exit - try to continue
        try:
            screen = pygame.display.set_mode(HDMI1_SIZE)
            sys.stderr.write("HDMI1: Using windowed mode as fallback\n")
            sys.stderr.flush()
        except:
            sys.stderr.write("HDMI1: Complete failure, exiting\n")
            sys.stderr.flush()
            sys.exit(1)
    
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    media_dir = os.path.join(project_root, 'media')
    image_path = os.environ.get('HDMI1_MEDIA_PATH') or os.path.join(media_dir, 'Blank.jpg')
    if not os.path.exists(image_path):
        image_path = os.path.join(media_dir, 'placeholder2.png')

    try:
        img = Image.open(image_path)
        
        # Convert RGBA to RGB if needed
        if img.mode == 'RGBA':
            background = Image.new('RGB', img.size, (0, 0, 0))
            background.paste(img, mask=img.split()[3])
            img = background
        elif img.mode != 'RGB':
            img = img.convert('RGB')
        
        # Scale to fit while maintaining aspect ratio
        img.thumbnail(HDMI1_SIZE, Image.Resampling.LANCZOS)
        
        # Create black background
        background = Image.new('RGB', HDMI1_SIZE, color='black')
        x_offset = (HDMI1_SIZE[0] - img.size[0]) // 2
        y_offset = (HDMI1_SIZE[1] - img.size[1]) // 2
        background.paste(img, (x_offset, y_offset))
        
        # Convert to pygame surface
        img_str = background.tobytes()
        content = pygame.image.fromstring(img_str, HDMI1_SIZE, 'RGB')
        
    except Exception as e:
        sys.stderr.write(f"Error loading {image_path}: {e}\n")
        content = pygame.Surface(HDMI1_SIZE)
        content.fill((0, 0, 0))
    
    # Main loop
    clock = pygame.time.Clock()
    running = True
    
    try:
        while running:
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    running = False
                elif event.type == pygame.KEYDOWN:
                    if event.key == pygame.K_ESCAPE:
                        running = False
            
            # Display content
            screen.blit(content, (0, 0))
            pygame.display.flip()
            clock.tick(30)
            
    except KeyboardInterrupt:
        pass
    finally:
        pygame.quit()

if __name__ == "__main__":
    main()

