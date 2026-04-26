import os
import shutil
from jinja2 import Environment, FileSystemLoader
import markdown
import yaml
import re
import glob
from pathlib import Path
import hashlib
import base64
import json
import datetime
import argparse
from urllib.parse import urljoin, urlparse
from urllib.request import urlopen
from urllib.error import URLError, HTTPError
from bs4 import BeautifulSoup
# Lazy import for optional dependencies
EBOOKLIB_AVAILABLE = False
MINIFICATION_AVAILABLE = False

# Global flags for chapter inclusion
INCLUDE_DRAFTS = False
INCLUDE_SCHEDULED = False


def _check_ebooklib():
    global EBOOKLIB_AVAILABLE
    try:
        import ebooklib
        from ebooklib import epub
        EBOOKLIB_AVAILABLE = True
        return True
    except ImportError:
        EBOOKLIB_AVAILABLE = False
        return False

def _check_minification():
    global MINIFICATION_AVAILABLE
    try:
        import htmlmin
        import rcssmin
        import rjsmin
        MINIFICATION_AVAILABLE = True
        return True
    except ImportError:
        MINIFICATION_AVAILABLE = False
        return False

BUILD_DIR = os.path.abspath("./build")
CONTENT_DIR = "./content"
PAGES_DIR = "./pages"
TEMPLATES_DIR = "./templates"
STATIC_DIR = "./static"

# Global template environment (will be enhanced with novel-specific support)
env = Environment(loader=FileSystemLoader(TEMPLATES_DIR))

# Global asset map for cache busting
ASSET_MAP = {}

def asset_url(filename):
    """Convert asset filename to cache-busted version if available"""
    return ASSET_MAP.get(filename, filename)

# Register the asset_url filter
env.filters['asset_url'] = asset_url

# Cache for novel-specific template environments
_novel_template_envs = {}

def get_novel_template_directories(novel_slug):
    """Get list of template directories for a novel (novel-specific first, then defaults)"""
    directories = []
    
    # Novel-specific templates directory
    novel_templates_dir = os.path.join(CONTENT_DIR, novel_slug, "templates")
    if os.path.exists(novel_templates_dir):
        directories.append(novel_templates_dir)
    
    # Default templates directory (fallback)
    directories.append(TEMPLATES_DIR)
    
    return directories

def get_novel_template_env(novel_slug):
    """Get or create a Jinja2 environment for a specific novel with template override support"""
    if novel_slug not in _novel_template_envs:
        template_dirs = get_novel_template_directories(novel_slug)
        loader = FileSystemLoader(template_dirs)
        novel_env = Environment(loader=loader)
        
        # Add the same filters as the global environment
        novel_env.filters['slugify_tag'] = slugify_tag
        novel_env.filters['format_date_for_display'] = format_date_for_display
        novel_env.filters['find_author_username'] = find_author_username_filter
        novel_env.filters['asset_url'] = asset_url
        
        # Note: is_chapter_new filter will be set per render with proper config
        
        _novel_template_envs[novel_slug] = novel_env
    
    return _novel_template_envs[novel_slug]

def check_novel_has_custom_templates(novel_slug):
    """Check if a novel has any custom templates"""
    novel_templates_dir = os.path.join(CONTENT_DIR, novel_slug, "templates")
    return os.path.exists(novel_templates_dir) and bool(os.listdir(novel_templates_dir))

def list_novel_custom_templates(novel_slug):
    """List all custom templates for a novel"""
    novel_templates_dir = os.path.join(CONTENT_DIR, novel_slug, "templates")
    if not os.path.exists(novel_templates_dir):
        return []
    
    custom_templates = []
    for file in os.listdir(novel_templates_dir):
        if file.endswith('.html'):
            custom_templates.append(file)
    
    return sorted(custom_templates)

def encrypt_content_with_password(content, password):
    """Encrypt content using XOR with SHA256 hash of password"""
    # Create SHA256 hash of password for consistent key
    key = hashlib.sha256(password.encode('utf-8')).digest()
    
    # Convert content to bytes
    content_bytes = content.encode('utf-8')
    
    # XOR encrypt
    encrypted = bytearray()
    for i, byte in enumerate(content_bytes):
        encrypted.append(byte ^ key[i % len(key)])
    
    # Return base64 encoded encrypted content
    return base64.b64encode(encrypted).decode('utf-8')

def create_password_verification_hash(password):
    """Create a verification hash that can be checked client-side"""
    # Use a simple hash that can be reproduced in JavaScript
    return hashlib.sha256(password.encode('utf-8')).hexdigest()[:16]

def build_footer_content(site_config, novel_config=None, page_type='site'):
    """Build footer content based on site and story configurations"""
    footer_data = {}
    
    # Determine copyright text
    if novel_config and novel_config.get('footer', {}).get('custom_text'):
        footer_data['copyright'] = novel_config['footer']['custom_text']
    elif novel_config and novel_config.get('copyright'):
        footer_data['copyright'] = novel_config['copyright']
    else:
        site_name = site_config.get('site_name', 'Web Novel Collection')
        footer_data['copyright'] = f"© 2025 {site_name}"
    
    # Build footer links
    footer_links = []
    
    # Add story-specific links if available
    if novel_config and novel_config.get('footer', {}).get('links'):
        footer_links.extend(novel_config['footer']['links'])
    
    # Add site-wide footer links if available
    if site_config.get('footer', {}).get('links'):
        footer_links.extend(site_config['footer']['links'])
    
    footer_data['links'] = footer_links
    
    # Add additional footer text
    if site_config.get('footer', {}).get('additional_text'):
        footer_data['additional_text'] = site_config['footer']['additional_text']
    
    return footer_data

def generate_rss_feed(site_config, novels_data, novel_config=None, novel_slug=None):
    """Generate RSS feed for site or specific story"""
    from datetime import datetime, timezone
    
    site_url = site_config.get('site_url', '').rstrip('/')
    site_name = site_config.get('site_name', 'Web Novel Collection')
    
    if novel_config and novel_slug:
        # Story-specific RSS feed
        feed_title = novel_config.get('title', 'Web Novel')
        feed_description = novel_config.get('description', 'Web Novel RSS Feed')
        feed_link = f"{site_url}/{novel_slug}/"
        feed_items = []
        
        # Get chapters for this novel
        available_languages = get_available_languages(novel_slug)
        primary_lang = novel_config.get('primary_language', 'en')
        
        all_chapters = []
        for arc in novel_config.get("arcs", []):
            all_chapters.extend(arc.get("chapters", []))
        
        # Sort chapters by published date (most recent first)
        chapter_items = []
        for chapter in all_chapters:
            chapter_id = chapter["id"]
            try:
                chapter_content_md, chapter_metadata = load_chapter_content(novel_slug, chapter_id, primary_lang)
                if chapter_metadata is None:
                    continue
                
                # Skip draft chapters unless include_drafts is True
                if should_skip_chapter(chapter_metadata, INCLUDE_DRAFTS, INCLUDE_SCHEDULED):
                    continue
                
                # Skip hidden chapters, password-protected, or non-indexed chapters
                seo_config = chapter_metadata.get('seo') or {}
                seo_allow_indexing = seo_config.get('allow_indexing') if isinstance(seo_config, dict) else None
                if (is_chapter_hidden(chapter_metadata) or 
                    ('password' in chapter_metadata and chapter_metadata['password']) or
                    seo_allow_indexing is False):
                    continue
                
                published_date = chapter_metadata.get('published')
                if published_date:
                    try:
                        # Use the parse_publish_date function for better date format support
                        pub_datetime = parse_publish_date(published_date)
                        if not pub_datetime:
                            continue  # Skip if date parsing failed
                        
                        # Normalize to timezone-naive datetime for consistent RSS sorting
                        if pub_datetime.tzinfo is not None:
                            pub_datetime = pub_datetime.replace(tzinfo=None)
                        
                        # Handle social_embeds safely
                        social_embeds = chapter_metadata.get('social_embeds') or {}
                        description = social_embeds.get('description', '') if isinstance(social_embeds, dict) else ''
                        
                        chapter_items.append({
                            'id': chapter_id,
                            'title': chapter_metadata.get('title', chapter['title']),
                            'link': f"{site_url}/{novel_slug}/{primary_lang}/{chapter_id}/",
                            'description': description,
                            'pub_date': pub_datetime,
                            'content': convert_markdown_to_html(chapter_content_md[:500] + '...' if len(chapter_content_md) > 500 else chapter_content_md)
                        })
                    except Exception as e:
                        pass  # Skip chapters with invalid dates
            except:
                continue
        
        # Sort by date (newest first) and take latest 20
        chapter_items.sort(key=lambda x: x['pub_date'], reverse=True)
        feed_items = chapter_items[:20]
        
    else:
        # Site-wide RSS feed
        feed_title = site_name
        feed_description = site_config.get('site_description', 'Web Novel Collection RSS Feed')
        feed_link = site_url
        feed_items = []
        
        # Collect recent chapters from all novels
        all_chapter_items = []
        for novel in novels_data:
            novel_slug = novel['slug']
            novel_config = load_novel_config(novel_slug)
            
            # Skip novels that don't allow indexing
            if novel_config.get('seo', {}).get('allow_indexing') is False:
                continue
            
            primary_lang = novel_config.get('primary_language', 'en')
            
            all_chapters = []
            for arc in novel.get("arcs", []):
                all_chapters.extend(arc.get("chapters", []))
            
            for chapter in all_chapters:
                chapter_id = chapter["id"]
                try:
                    chapter_content_md, chapter_metadata = load_chapter_content(novel_slug, chapter_id, primary_lang)
                    
                    # Skip draft chapters unless include_drafts is True
                    if should_skip_chapter(chapter_metadata, INCLUDE_DRAFTS, INCLUDE_SCHEDULED):
                        continue
                    
                    # Skip hidden, password-protected, or non-indexed chapters
                    seo_config = chapter_metadata.get('seo') or {}
                    seo_allow_indexing = seo_config.get('allow_indexing') if isinstance(seo_config, dict) else None
                    if (is_chapter_hidden(chapter_metadata) or 
                        ('password' in chapter_metadata and chapter_metadata['password']) or
                        seo_allow_indexing is False):
                        continue
                    
                    published_date = chapter_metadata.get('published')
                    if published_date:
                        try:
                            # Use the parse_publish_date function for better date format support
                            pub_datetime = parse_publish_date(published_date)
                            if not pub_datetime:
                                continue  # Skip if date parsing failed
                            
                            # Normalize to timezone-naive datetime for consistent RSS sorting
                            if pub_datetime.tzinfo is not None:
                                pub_datetime = pub_datetime.replace(tzinfo=None)
                            
                            # Handle social_embeds safely for site-wide RSS
                            social_embeds = chapter_metadata.get('social_embeds') or {}
                            description = social_embeds.get('description', '') if isinstance(social_embeds, dict) else ''
                            
                            all_chapter_items.append({
                                'id': chapter_id,
                                'title': f"{novel.get('title', '')}: {chapter_metadata.get('title', chapter['title'])}",
                                'link': f"{site_url}/{novel_slug}/{primary_lang}/{chapter_id}/",
                                'description': description,
                                'pub_date': pub_datetime,
                                'content': convert_markdown_to_html(chapter_content_md[:300] + '...' if len(chapter_content_md) > 300 else chapter_content_md)
                            })
                        except:
                            pass
                except:
                    continue
        
        # Sort by date (newest first) and take latest 50
        all_chapter_items.sort(key=lambda x: x['pub_date'], reverse=True)
        feed_items = all_chapter_items[:50]
    
    # Build RSS XML using timezone-aware dates to satisfy RSS spec
    current_time = datetime.now(timezone.utc)
    
    rss_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/" xmlns:atom="http://www.w3.org/2005/Atom">
<channel>
    <title>{feed_title}</title>
    <link>{feed_link}</link>
    <description>{feed_description}</description>
    <language>en-us</language>
    <lastBuildDate>{current_time.strftime('%a, %d %b %Y %H:%M:%S %z')}</lastBuildDate>
    <generator>Web Novel Static Generator</generator>
"""
    
    for item in feed_items:
        pub_date_str = (
            item['pub_date'].replace(tzinfo=timezone.utc).strftime('%a, %d %b %Y %H:%M:%S %z')
            if item['pub_date'] else ''
        )
        
        rss_content += f"""    <item>
        <title>{item['title']}</title>
        <link>{item['link']}</link>
        <description><![CDATA[{item['description']}]]></description>
        <content:encoded><![CDATA[{item['content']}]]></content:encoded>
        <pubDate>{pub_date_str}</pubDate>
        <guid>{item['link']}</guid>
    </item>
"""
    
    rss_content += """</channel>
</rss>"""
    
    return rss_content

def generate_sitemap_xml(site_config, novels_data):
    """Generate sitemap.xml file for SEO"""
    from datetime import datetime
    
    sitemap_entries = []
    site_url = site_config.get('site_url', '').rstrip('/')
    
    if not site_url:
        return "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n<urlset xmlns=\"http://www.sitemaps.org/schemas/sitemap/0.9\">\n</urlset>"
    
    # Add front page
    sitemap_entries.append(f"""    <url>
        <loc>{site_url}/</loc>
        <changefreq>weekly</changefreq>
        <priority>1.0</priority>
    </url>""")
    
    # Add page index
    available_languages = site_config.get('languages', {}).get('available', ['en'])
    for lang in available_languages:
        index_filename = f"pages-{lang}.html" if lang != 'en' else "pages.html"
        sitemap_entries.append(f"""    <url>
        <loc>{site_url}/{index_filename}</loc>
        <changefreq>weekly</changefreq>
        <priority>0.7</priority>
    </url>""")
    
    # Add static pages
    all_pages = get_all_pages()
    available_languages = site_config.get('languages', {}).get('available', ['en'])
    
    for page_data in all_pages:
        page_slug = page_data['slug']
        # Load page metadata to check if it should be included
        for lang in available_languages:
            try:
                _, page_metadata = load_page_content(page_slug, lang)
                
                # Skip pages that don't allow indexing, are drafts, or are password-protected
                if should_skip_page(page_metadata, INCLUDE_DRAFTS):
                    continue
                    
                page_allow_indexing = page_metadata.get('seo', {}).get('allow_indexing')
                is_password_protected = 'password' in page_metadata and page_metadata['password']
                
                if page_allow_indexing is False or is_password_protected:
                    continue
                
                # Build the page URL
                if '/' in page_slug:
                    # Nested page (e.g., "resources/translation-guide")
                    page_url = f"{site_url}/{page_slug}/{lang}/"
                else:
                    # Top-level page (e.g., "about")
                    page_url = f"{site_url}/{page_slug}/{lang}/"
                
                # Get updated date if available
                lastmod = ""
                if page_metadata.get('updated'):
                    try:
                        from datetime import datetime
                        update_date = datetime.strptime(page_metadata['updated'], '%Y-%m-%d')
                        lastmod = f"\n        <lastmod>{update_date.strftime('%Y-%m-%d')}</lastmod>"
                    except:
                        pass
                
                sitemap_entries.append(f"""    <url>
        <loc>{page_url}</loc>
        <changefreq>monthly</changefreq>
        <priority>0.6</priority>{lastmod}
    </url>""")
                    
            except:
                # Skip pages that don't exist for this language
                continue
    
    # Add novel pages
    for novel in novels_data:
        novel_slug = novel['slug']
        novel_config = load_novel_config(novel_slug)
        
        # Skip novels that don't allow indexing
        if novel_config.get('seo', {}).get('allow_indexing') is False:
            continue
            
        available_languages = get_available_languages(novel_slug)
        
        for lang in available_languages:
            # Add TOC pages
            sitemap_entries.append(f"""    <url>
        <loc>{site_url}/{novel_slug}/{lang}/toc/</loc>
        <changefreq>weekly</changefreq>
        <priority>0.8</priority>
    </url>""")
            
            # Add tag index pages
            sitemap_entries.append(f"""    <url>
        <loc>{site_url}/{novel_slug}/{lang}/tags/</loc>
        <changefreq>monthly</changefreq>
        <priority>0.6</priority>
    </url>""")
            
            # Add individual chapters
            all_chapters = []
            for arc in novel.get("arcs", []):
                all_chapters.extend(arc.get("chapters", []))
            
            for chapter in all_chapters:
                chapter_id = chapter["id"]
                try:
                    chapter_content_md, chapter_metadata = load_chapter_content(novel_slug, chapter_id, lang)
                    
                    # Skip draft chapters unless include_drafts is True
                    if should_skip_chapter(chapter_metadata, INCLUDE_DRAFTS, INCLUDE_SCHEDULED):
                        continue
                    
                    # Skip chapters that don't allow indexing, are password-protected, or are hidden
                    chapter_allow_indexing = chapter_metadata.get('seo', {}).get('allow_indexing')
                    is_password_protected = 'password' in chapter_metadata and chapter_metadata['password']
                    is_hidden = is_chapter_hidden(chapter_metadata)
                    
                    if chapter_allow_indexing is False or is_password_protected or is_hidden:
                        continue
                    
                    # Get published date if available
                    lastmod = ""
                    if chapter_metadata.get('published'):
                        try:
                            # Use parse_publish_date for better date format support
                            pub_date = parse_publish_date(chapter_metadata['published'])
                            if pub_date:
                                lastmod = f"\n        <lastmod>{pub_date.strftime('%Y-%m-%d')}</lastmod>"
                        except:
                            pass
                    
                    sitemap_entries.append(f"""    <url>
        <loc>{site_url}/{novel_slug}/{lang}/{chapter_id}/</loc>
        <changefreq>monthly</changefreq>
        <priority>0.7</priority>{lastmod}
    </url>""")
                    
                except:
                    # Skip chapters that don't exist for this language
                    continue
            
            # Add tag pages
            tags_data = collect_tags_for_novel(novel_slug, lang)
            for tag in tags_data.keys():
                tag_slug = slugify_tag(tag)
                sitemap_entries.append(f"""    <url>
        <loc>{site_url}/{novel_slug}/{lang}/tags/{tag_slug}/</loc>
        <changefreq>monthly</changefreq>
        <priority>0.5</priority>
    </url>""")
    
    sitemap_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
{chr(10).join(sitemap_entries)}
</urlset>"""
    
    return sitemap_content

def generate_robots_txt(site_config, novels_data):
    """Generate robots.txt file based on site and story configurations"""
    robots_content = ["# Robots.txt for Web Novel Static Generator"]
    
    # Add sitemap reference
    site_url = site_config.get('site_url', '').rstrip('/')
    if site_url:
        robots_content.append(f"Sitemap: {site_url}/sitemap.xml")
        robots_content.append("")
    
    # Check site-wide indexing settings
    site_allow_indexing = site_config.get('seo', {}).get('allow_indexing', True)
    
    if not site_allow_indexing:
        # If site doesn't allow indexing, disallow all
        robots_content.extend([
            "User-agent: *",
            "Disallow: /",
            ""
        ])
    else:
        robots_content.extend([
            "User-agent: *",
            "Allow: /",
            ""
        ])
        
        # Add disallow rules for specific novels or chapters that don't allow indexing
        disallowed_paths = []
        
        for novel in novels_data:
            novel_slug = novel['slug']
            novel_config = load_novel_config(novel_slug)
            
            # Check novel-level indexing settings
            novel_allow_indexing = novel_config.get('seo', {}).get('allow_indexing')
            if novel_allow_indexing is False:
                disallowed_paths.append(f"Disallow: /{novel_slug}/")
                continue
            
            # Check individual chapters for indexing settings
            available_languages = get_available_languages(novel_slug)
            for lang in available_languages:
                all_chapters = []
                for arc in novel.get("arcs", []):
                    all_chapters.extend(arc.get("chapters", []))
                
                for chapter in all_chapters:
                    chapter_id = chapter["id"]
                    try:
                        chapter_content_md, chapter_metadata = load_chapter_content(novel_slug, chapter_id, lang)
                        
                        # Skip draft chapters unless include_drafts is True
                        if should_skip_chapter(chapter_metadata, INCLUDE_DRAFTS, INCLUDE_SCHEDULED):
                            continue
                        
                        # Check chapter-level indexing
                        seo_config = chapter_metadata.get('seo') or {}
                        chapter_allow_indexing = seo_config.get('allow_indexing') if isinstance(seo_config, dict) else None
                        if chapter_allow_indexing is False:
                            disallowed_paths.append(f"Disallow: /{novel_slug}/{lang}/{chapter_id}/")
                        
                        # Also disallow password-protected and hidden content
                        if 'password' in chapter_metadata and chapter_metadata['password']:
                            disallowed_paths.append(f"Disallow: /{novel_slug}/{lang}/{chapter_id}/")
                        
                        # Disallow hidden chapters
                        if is_chapter_hidden(chapter_metadata):
                            disallowed_paths.append(f"Disallow: /{novel_slug}/{lang}/{chapter_id}/")
                            
                    except:
                        # Skip chapters that don't exist for this language
                        continue
        
        # Add all disallow rules
        if disallowed_paths:
            robots_content.extend(disallowed_paths)
            robots_content.append("")
    
    robots_content.append("# Generated by Web Novel Static Generator")
    
    return "\n".join(robots_content)

def load_site_config():
    """Load global site configuration"""
    config_file = "site_config.yaml"
    if os.path.exists(config_file):
        with open(config_file, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f)
    return {}

def build_social_meta(site_config, novel_config, chapter_metadata, page_type, title, url):
    """Build social media metadata for a page"""
    social_meta = {}
    
    # Handle None chapter_metadata
    if chapter_metadata is None:
        chapter_metadata = {}
    
    # Determine social title
    if chapter_metadata and 'social_embeds' in chapter_metadata and chapter_metadata['social_embeds'] and 'title' in chapter_metadata['social_embeds']:
        social_meta['title'] = chapter_metadata['social_embeds']['title']
    elif page_type == 'chapter':
        # Include story name in chapter social titles like website titles do
        novel_title = novel_config.get('title', '') if novel_config else ''
        if novel_title:
            social_meta['title'] = f"{title} - {novel_title}"
        else:
            social_meta['title'] = title
    elif page_type == 'toc':
        social_meta['title'] = f"{novel_config.get('title', '')} - Table of Contents"
    else:
        social_meta['title'] = title
    
    # Apply title format if specified
    title_format = site_config.get('social_embeds', {}).get('title_format', '{title}')
    social_meta['title'] = title_format.format(title=social_meta['title'])
    
    # Determine social description
    if chapter_metadata and 'social_embeds' in chapter_metadata and chapter_metadata['social_embeds'] and 'description' in chapter_metadata['social_embeds']:
        social_meta['description'] = chapter_metadata['social_embeds']['description']
    elif novel_config and novel_config.get('social_embeds', {}).get('description'):
        social_meta['description'] = novel_config['social_embeds']['description']
    else:
        social_meta['description'] = site_config.get('social_embeds', {}).get('default_description', site_config.get('site_description', ''))
    
    # Determine social image (absolute URL)
    site_url = site_config.get('site_url', '').rstrip('/')
    if chapter_metadata and 'social_embeds' in chapter_metadata and chapter_metadata['social_embeds'] and 'image' in chapter_metadata['social_embeds']:
        image_path = chapter_metadata['social_embeds']['image']
    elif novel_config and novel_config.get('social_embeds', {}).get('image'):
        image_path = novel_config['social_embeds']['image']
    else:
        image_path = site_config.get('social_embeds', {}).get('default_image', '/static/images/default-social.jpg')
    
    # Convert to absolute URL if relative
    if image_path.startswith('/'):
        social_meta['image'] = site_url + image_path
    else:
        social_meta['image'] = image_path
    
    # Set URL
    social_meta['url'] = url
    
    # Build keywords
    keywords = []
    if chapter_metadata and 'social_embeds' in chapter_metadata and chapter_metadata['social_embeds'] and 'keywords' in chapter_metadata['social_embeds']:
        keywords.extend(chapter_metadata['social_embeds']['keywords'])
    elif novel_config and novel_config.get('social_embeds', {}).get('keywords'):
        keywords.extend(novel_config['social_embeds']['keywords'])
    
    social_meta['keywords'] = ', '.join(keywords) if keywords else None
    
    return social_meta

def generate_image_hash(file_path, length=8):
    """Generate a partial hash of an image file for consistent naming"""
    hasher = hashlib.sha256()
    with open(file_path, 'rb') as f:
        # Read in chunks to handle large files efficiently
        for chunk in iter(lambda: f.read(4096), b""):
            hasher.update(chunk)
    return hasher.hexdigest()[:length]

def build_seo_meta(site_config, novel_config, chapter_metadata, page_type):
    """Build SEO metadata for a page"""
    seo_meta = {}
    
    # Handle None chapter_metadata
    if chapter_metadata is None:
        chapter_metadata = {}
    
    # Determine if indexing is allowed (chapter > story > site)
    if 'seo' in chapter_metadata and 'allow_indexing' in chapter_metadata['seo']:
        seo_meta['allow_indexing'] = chapter_metadata['seo']['allow_indexing']
    elif novel_config and novel_config.get('seo', {}).get('allow_indexing') is not None:
        seo_meta['allow_indexing'] = novel_config['seo']['allow_indexing']
    else:
        seo_meta['allow_indexing'] = site_config.get('seo', {}).get('allow_indexing', True)
    
    # Determine meta description
    if 'seo' in chapter_metadata and 'meta_description' in chapter_metadata['seo']:
        seo_meta['meta_description'] = chapter_metadata['seo']['meta_description']
    elif novel_config and novel_config.get('seo', {}).get('meta_description'):
        seo_meta['meta_description'] = novel_config['seo']['meta_description']
    else:
        seo_meta['meta_description'] = site_config.get('site_description', '')
    
    return seo_meta

def should_minify(serve_mode=False, no_minify=False):
    """Determine if minification should be applied"""
    # Don't minify in serve mode (development) unless explicitly enabled
    if serve_mode:
        return False
    # Respect explicit --no-minify flag
    if no_minify:
        return False
    # Check if minification libraries are available
    if not _check_minification():
        return False
    return True

def minify_html_content(html_content):
    """Minify HTML content while preserving important formatting"""
    if not MINIFICATION_AVAILABLE:
        return html_content
    
    try:
        import htmlmin
        return htmlmin.minify(
            html_content,
            remove_comments=True,
            remove_empty_space=True,
            reduce_boolean_attributes=True,
            # Preserve formatting in specific elements
            keep_pre=True  # Preserve <pre> content
        )
    except Exception as e:
        print(f"    Warning: HTML minification failed: {e}")
        return html_content

def minify_css_content(css_content):
    """Minify CSS content"""
    if not MINIFICATION_AVAILABLE:
        return css_content
    
    try:
        import rcssmin
        return rcssmin.cssmin(css_content)
    except Exception as e:
        print(f"    Warning: CSS minification failed: {e}")
        return css_content

def minify_js_content(js_content):
    """Minify JavaScript content"""
    if not MINIFICATION_AVAILABLE:
        return js_content
    
    try:
        import rjsmin
        return rjsmin.jsmin(js_content)
    except Exception as e:
        print(f"    Warning: JavaScript minification failed: {e}")
        return js_content

def write_html_file(file_path, html_content, minify=False):
    """Write HTML content to file with optional minification"""
    if minify:
        html_content = minify_html_content(html_content)
    
    with open(file_path, "w", encoding='utf-8') as f:
        f.write(html_content)

def process_cover_art(novel_slug, novel_config):
    """Process cover art images by copying them to static/images with hash-based filenames"""
    processed_images = {}
    
    # Ensure static/images directory exists
    images_dir = os.path.normpath(os.path.join(BUILD_DIR, "static", "images"))
    os.makedirs(images_dir, exist_ok=True)
    
    # Process story cover art
    if novel_config.get('front_page', {}).get('cover_art'):
        source_path = os.path.join(CONTENT_DIR, novel_slug, novel_config['front_page']['cover_art'])
        if os.path.exists(source_path):
            # Generate hash-based filename with original name
            original_filename = os.path.basename(source_path)
            file_name, file_extension = os.path.splitext(original_filename)
            file_hash = generate_image_hash(source_path)
            unique_filename = f"{file_hash}-{file_name}{file_extension}"
            dest_path = os.path.join(images_dir, unique_filename)
            
            # Copy the image
            shutil.copy2(source_path, dest_path)
            
            # Store the processed path
            processed_images['story_cover'] = f"static/images/{unique_filename}"
    
    # Process arc cover art
    if novel_config.get('arcs'):
        for i, arc in enumerate(novel_config['arcs']):
            if arc.get('cover_art'):
                source_path = os.path.join(CONTENT_DIR, novel_slug, arc['cover_art'])
                if os.path.exists(source_path):
                    # Generate hash-based filename with original name
                    original_filename = os.path.basename(source_path)
                    file_name, file_extension = os.path.splitext(original_filename)
                    file_hash = generate_image_hash(source_path)
                    unique_filename = f"{file_hash}-{file_name}{file_extension}"
                    dest_path = os.path.join(images_dir, unique_filename)
                    
                    # Copy the image
                    shutil.copy2(source_path, dest_path)
                    
                    # Store the processed path
                    processed_images[f'arc_{i}_cover'] = f"static/images/{unique_filename}"
    
    return processed_images

def load_authors_config():
    """Load authors configuration from authors.yaml"""
    authors_file = "authors.yaml"
    if os.path.exists(authors_file):
        with open(authors_file, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)
            return config.get('authors', {})
    return {}

def find_author_username(author_name, authors_config):
    """Find the username for an author by their display name"""
    for username, author_info in authors_config.items():
        if author_info.get('name') == author_name:
            return username
    return None

def collect_author_contributions(all_novels_data):
    """Collect all stories and chapters that each author contributed to"""
    author_contributions = {}
    
    for novel in all_novels_data:
        novel_slug = novel['slug']
        novel_title = novel.get('title', novel_slug)
        novel_config = load_novel_config(novel_slug)
        
        # Check story-level author
        story_author = novel_config.get('author', {}).get('name')
        if story_author:
            if story_author not in author_contributions:
                author_contributions[story_author] = {'stories': [], 'chapters': []}
            author_contributions[story_author]['stories'].append({
                'slug': novel_slug,
                'title': novel_title,
                'description': novel.get('description'),
                'role': 'Author'
            })
        
        # Check each chapter for author/translator contributions (use primary language only to avoid duplicates)
        primary_lang = novel_config.get('primary_language', 'en')
        for arc in novel.get('arcs', []):
            for chapter in arc.get('chapters', []):
                chapter_id = chapter['id']
                chapter_title = chapter['title']
                
                # Load chapter content to get front matter (use primary language only)
                try:
                    chapter_content, chapter_metadata = load_chapter_content(novel_slug, chapter_id, primary_lang)
                    
                    # Check chapter author
                    if chapter_metadata.get('author'):
                        author_name = chapter_metadata['author']
                        if author_name not in author_contributions:
                            author_contributions[author_name] = {'stories': [], 'chapters': []}
                        author_contributions[author_name]['chapters'].append({
                            'novel_slug': novel_slug,
                            'novel_title': novel_title,
                            'chapter_id': chapter_id,
                            'title': chapter_title,
                            'role': 'Author',
                            'published': chapter_metadata.get('published')
                        })
                    
                    # Check chapter translator
                    if chapter_metadata.get('translator'):
                        translator_name = chapter_metadata['translator']
                        if translator_name not in author_contributions:
                            author_contributions[translator_name] = {'stories': [], 'chapters': []}
                        author_contributions[translator_name]['chapters'].append({
                            'novel_slug': novel_slug,
                            'novel_title': novel_title,
                            'chapter_id': chapter_id,
                            'title': chapter_title,
                            'role': 'Translator',
                            'published': chapter_metadata.get('published')
                        })
                except:
                    # Skip chapters that can't be loaded
                    continue
    
    return author_contributions

def get_non_hidden_chapters(novel_config, novel_slug, language='en', include_drafts=False, include_scheduled=False):
    """Get list of chapters that are not hidden or drafts"""
    visible_chapters = []
    
    for arc in novel_config.get('arcs', []):
        arc_chapters = []
        for chapter in arc.get('chapters', []):
            chapter_id = chapter['id']
            
            # Load chapter content to check if it's hidden, password protected, or draft
            try:
                chapter_content, chapter_metadata = load_chapter_content(novel_slug, chapter_id, language)
                
                # Skip if chapter should be skipped
                if should_skip_chapter(chapter_metadata, include_drafts, include_scheduled):
                    continue
                
                arc_chapters.append({
                    'id': chapter_id,
                    'title': chapter['title'],
                    'content': chapter_content,
                    'metadata': chapter_metadata
                })
            except:
                # Skip chapters that can't be loaded
                continue
        
        if arc_chapters:  # Only include arcs with visible chapters
            visible_chapters.append({
                'title': arc['title'],
                'cover_art': arc.get('cover_art'),
                'chapters': arc_chapters
            })
    
    return visible_chapters

def get_chapters_for_epub(novel_config, novel_slug, language='en', include_drafts=False, include_scheduled=False):
    """Get list of chapters for EPUB generation (excludes hidden, draft, and password-protected)"""
    visible_chapters = []
    
    for arc in novel_config.get('arcs', []):
        arc_chapters = []
        for chapter in arc.get('chapters', []):
            chapter_id = chapter['id']
            
            # Load chapter content to check if it should be included in EPUB
            try:
                chapter_content, chapter_metadata = load_chapter_content(novel_slug, chapter_id, language)
                
                if chapter_content is None:
                    continue
                
                # Skip if chapter should be skipped in EPUB
                if should_skip_chapter_in_epub(chapter_metadata, include_drafts):
                    continue
                
                arc_chapters.append({
                    'id': chapter_id,
                    'title': chapter['title'],
                    'content': chapter_content,
                    'metadata': chapter_metadata
                })
            except:
                # Skip chapters that can't be loaded
                continue
        
        if arc_chapters:  # Only include arcs with visible chapters
            visible_chapters.append({
                'title': arc['title'],
                'cover_art': arc.get('cover_art'),
                'chapters': arc_chapters
            })
    
    return visible_chapters


def load_page_content(page_slug, language='en'):
    """Load page content from markdown file with language support and front matter parsing"""
    # Try language-specific file first
    if language != 'en':
        page_file = os.path.join(PAGES_DIR, language, f"{page_slug}.md")
        if os.path.exists(page_file):
            with open(page_file, 'r', encoding='utf-8') as f:
                content = f.read()
                front_matter, markdown_content = parse_front_matter(content)
                return markdown_content, front_matter
    
    # Fallback to default language file
    page_file = os.path.join(PAGES_DIR, f"{page_slug}.md")
    if os.path.exists(page_file):
        with open(page_file, 'r', encoding='utf-8') as f:
            content = f.read()
            front_matter, markdown_content = parse_front_matter(content)
            return markdown_content, front_matter
    
    return None, {}

def load_nested_page_content(page_path, language='en'):
    """Load nested page content (e.g., resources/translation-guide)"""
    page_slug = page_path.replace('/', os.sep)
    
    # Try language-specific file first
    if language != 'en':
        page_file = os.path.join(PAGES_DIR, language, f"{page_slug}.md")
        if os.path.exists(page_file):
            with open(page_file, 'r', encoding='utf-8') as f:
                content = f.read()
                front_matter, markdown_content = parse_front_matter(content)
                return markdown_content, front_matter
    
    # Fallback to default language file
    page_file = os.path.join(PAGES_DIR, f"{page_slug}.md")
    if os.path.exists(page_file):
        with open(page_file, 'r', encoding='utf-8') as f:
            content = f.read()
            front_matter, markdown_content = parse_front_matter(content)
            return markdown_content, front_matter
    
    return None, {}

def get_all_pages():
    """Get list of all available pages"""
    pages = []
    
    if not os.path.exists(PAGES_DIR):
        return pages
    
    def scan_pages_directory(directory, prefix=""):
        for item in os.listdir(directory):
            item_path = os.path.join(directory, item)
            
            if os.path.isfile(item_path) and item.endswith('.md'):
                page_slug = prefix + item[:-3]  # Remove .md extension
                try:
                    content, metadata = load_page_content(page_slug.replace(os.sep, '/'))
                    if content:
                        pages.append({
                            'slug': page_slug.replace(os.sep, '/'),
                            'title': metadata.get('title', page_slug),
                            'description': metadata.get('description', ''),
                            'metadata': metadata
                        })
                except:
                    continue
            elif os.path.isdir(item_path) and not item.startswith('.') and len(item) != 2:  # Ignore language dirs
                scan_pages_directory(item_path, prefix + item + "/")
    
    scan_pages_directory(PAGES_DIR)
    return pages

def get_available_page_languages(page_slug):
    """Get list of available languages for a page"""
    languages = ['en']  # Default language
    
    if not os.path.exists(PAGES_DIR):
        return languages
    
    # Check for language-specific versions
    for item in os.listdir(PAGES_DIR):
        item_path = os.path.join(PAGES_DIR, item)
        if os.path.isdir(item_path) and len(item) == 2:  # Assume 2-letter language codes
            page_file = os.path.join(item_path, f"{page_slug}.md")
            if os.path.exists(page_file):
                languages.append(item)
    
    return sorted(set(languages))

def should_skip_page(page_metadata, include_drafts=False):
    """Check if a page should be skipped during generation"""
    if page_metadata.get('hidden', False):
        return True
    if page_metadata.get('draft', False) and not include_drafts:
        return True
    return False

def build_page_navigation(site_config, current_language='en', current_page_slug=None):
    """Build navigation menus from static pages"""
    if not os.path.exists(PAGES_DIR):
        return {'header': [], 'footer': []}
    
    navigation = {'header': [], 'footer': []}
    all_pages = get_all_pages()
    
    # Filter pages for current language and collect nav items
    nav_items = {'header': [], 'footer': []}
    
    for page_data in all_pages:
        page_slug = page_data['slug']
        page_metadata = page_data['metadata']
        
        # Skip if page should be skipped
        if should_skip_page(page_metadata, INCLUDE_DRAFTS):
            continue
        
        # Check if page is available in current language
        page_languages = get_available_page_languages(page_slug)
        if current_language not in page_languages:
            continue
        
        # Check navigation placement
        nav_placement = page_metadata.get('navigation')
        if nav_placement in ['header', 'footer']:
            nav_order = page_metadata.get('nav_order', 999)
            
            nav_items[nav_placement].append({
                'title': page_metadata.get('title', page_slug),
                'url': f"{page_slug}/{current_language}/",
                'slug': page_slug,
                'order': nav_order,
                'active': page_slug == current_page_slug
            })
    
    # Sort by nav_order
    for placement in ['header', 'footer']:
        nav_items[placement].sort(key=lambda x: x['order'])
        navigation[placement] = nav_items[placement]
    
    return navigation

def load_webring_config():
    """Load webring configuration from webring.yaml"""
    webring_file = os.path.join(os.getcwd(), "webring.yaml")
    if os.path.exists(webring_file):
        with open(webring_file, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)
            return config.get('webring', {})
    return {}

def fetch_rss_feed(url, timeout=10):
    """Fetch and parse RSS feed from URL with comprehensive error handling"""
    try:
        with urlopen(url, timeout=timeout) as response:
            # Check response status
            if response.status != 200:
                print(f"    Warning: RSS feed returned status {response.status}: {url}")
                return None
                
            content = response.read().decode('utf-8')
            soup = BeautifulSoup(content, 'xml')
            
            # Verify it's actually an RSS/XML feed
            if not soup.find('rss') and not soup.find('feed'):
                print(f"    Warning: URL does not appear to be a valid RSS feed: {url}")
                return None
                
            return soup
    except (URLError, HTTPError) as e:
        print(f"    Warning: Network error fetching RSS feed from {url}: {e}")
        return None
    except UnicodeDecodeError as e:
        print(f"    Warning: Unable to decode RSS feed content from {url}: {e}")
        return None
    except Exception as e:
        print(f"    Warning: Unexpected error fetching RSS feed from {url}: {e}")
        return None

def parse_rss_items(rss_soup, site_name, site_url):
    """Parse RSS feed and extract items"""
    if not rss_soup:
        return []
    
    items = []
    for item in rss_soup.find_all('item'):
        title_elem = item.find('title')
        link_elem = item.find('link')
        pub_date_elem = item.find('pubDate')
        description_elem = item.find('description')
        
        if title_elem and link_elem:
            title = title_elem.get_text(strip=True)
            link = link_elem.get_text(strip=True)
            
            # Parse publication date
            pub_date = None
            if pub_date_elem:
                try:
                    date_str = pub_date_elem.get_text(strip=True)
                    # Try to parse common RSS date formats
                    for fmt in ['%a, %d %b %Y %H:%M:%S %z', '%a, %d %b %Y %H:%M:%S %Z', '%Y-%m-%dT%H:%M:%S%z']:
                        try:
                            pub_date = datetime.datetime.strptime(date_str, fmt)
                            break
                        except ValueError:
                            continue
                    if not pub_date:
                        # Fallback: try parsing without timezone
                        try:
                            pub_date = datetime.datetime.strptime(date_str[:25], '%a, %d %b %Y %H:%M:%S')
                        except ValueError:
                            pass
                except Exception:
                    pass
            
            # Extract description
            description = ""
            if description_elem:
                desc_text = description_elem.get_text(strip=True)
                # Limit description length
                if len(desc_text) > 150:
                    description = desc_text[:147] + "..."
                else:
                    description = desc_text
            
            items.append({
                'title': title,
                'link': link,
                'pub_date': pub_date,
                'description': description,
                'site_name': site_name,
                'site_url': site_url
            })
    
    return items

def generate_webring_data(webring_config, display_config):
    """Generate webring data by fetching and parsing RSS feeds"""
    if not webring_config.get('enabled', False):
        return []
    
    all_items = []
    max_items = webring_config.get('max_items', 20)
    sites_list = webring_config.get('sites') or []
    include_own_rss = webring_config.get('include_own_rss', False)
    
    if not sites_list and not include_own_rss:
        print("Webring enabled but no sites configured and own RSS not included")
        return []
    
    print("Fetching webring RSS feeds...")
    
    successful_sites = 0
    failed_sites = 0
    
    for site in sites_list:
        site_name = site.get('name', 'Unknown Site')
        site_url = site.get('url', '')
        rss_url = site.get('rss', '')
        
        if not rss_url:
            continue
        
        print(f"  Fetching {site_name}...")
        
        rss_soup = fetch_rss_feed(rss_url)
        if rss_soup:
            items = parse_rss_items(rss_soup, site_name, site_url)
            all_items.extend(items)
            successful_sites += 1
            print(f"    Found {len(items)} items from {site_name}")
        else:
            failed_sites += 1
            print(f"    Failed to fetch RSS from {site_name}")
    
    if include_own_rss:
        print("Including own RSS feed...")
        # Generate own RSS items (this would need to be implemented based on site data)
        # For now, skip this as it requires more context
        pass
    
    if successful_sites == 0:
        print(f"Warning: Failed to fetch any webring RSS feeds ({failed_sites} failures)")
        return []
    
    # Sort all items by publication date (newest first)
    all_items.sort(key=lambda x: x['pub_date'] if x['pub_date'] else datetime.datetime.min, reverse=True)
    
    # Limit to max_items
    all_items = all_items[:max_items]
    
    print(f"Webring data generated: {len(all_items)} items from {successful_sites} sites")
    
    return all_items

def parse_front_matter(content):
    """Parse YAML front matter from markdown content"""
    if content.startswith('---'):
        parts = content.split('---', 2)
        if len(parts) >= 3:
            front_matter_text = parts[1].strip()
            markdown_content = parts[2].strip()
            
            try:
                front_matter = yaml.safe_load(front_matter_text)
                if front_matter is None:
                    front_matter = {}
                return front_matter, markdown_content
            except yaml.YAMLError:
                # If YAML parsing fails, treat as regular markdown
                return {}, content
    
    return {}, content

def convert_markdown_to_html(markdown_content):
    """Convert markdown content to HTML"""
    # Configure markdown extensions
    md = markdown.Markdown(extensions=[
        'extra',           # Extra features like tables, footnotes
        'toc',            # Table of contents
        'codehilite',     # Syntax highlighting for code blocks
        'nl2br',          # Convert newlines to <br>
        'sane_lists',     # Better list handling
    ])
    
    # Convert markdown to HTML
    html_content = md.convert(markdown_content)
    return html_content

def load_novel_config(novel_slug):
    """Load novel configuration from config.yaml"""
    config_file = os.path.join(CONTENT_DIR, novel_slug, "config.yaml")
    if os.path.exists(config_file):
        with open(config_file, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f)
    return {}

def load_chapter_content(novel_slug, chapter_id, language='en'):
    """Load chapter content from markdown file with language support and front matter parsing"""
    # Try language-specific file first
    if language != 'en':
        chapter_file = os.path.join(CONTENT_DIR, novel_slug, "chapters", language, f"{chapter_id}.md")
        if os.path.exists(chapter_file):
            with open(chapter_file, 'r', encoding='utf-8') as f:
                content = f.read()
                front_matter, markdown_content = parse_front_matter(content)
                return markdown_content, front_matter
    
    # Fallback to default language file
    chapter_file = os.path.join(CONTENT_DIR, novel_slug, "chapters", f"{chapter_id}.md")
    if os.path.exists(chapter_file):
        with open(chapter_file, 'r', encoding='utf-8') as f:
            content = f.read()
            front_matter, markdown_content = parse_front_matter(content)
            return markdown_content, front_matter
    
    return None, {}

def get_available_languages(novel_slug):
    """Get list of available languages for a novel"""
    languages = ['en']  # Default language
    
    chapters_dir = os.path.join(CONTENT_DIR, novel_slug, "chapters")
    if not os.path.exists(chapters_dir):
        return languages
    
    # Check for language directories
    for item in os.listdir(chapters_dir):
        item_path = os.path.join(chapters_dir, item)
        if os.path.isdir(item_path) and len(item) == 2:  # Assume 2-letter language codes
            languages.append(item)
    
    return sorted(set(languages))

def should_skip_chapter(chapter_metadata, include_drafts=False, include_scheduled=False):
    """Check if a chapter should be skipped during generation"""
    if chapter_metadata is None:
        return True
    if chapter_metadata.get('hidden', False):
        return True
    if chapter_metadata.get('draft', False) and not include_drafts:
        return True
    if chapter_metadata.get('scheduled', False) and not include_scheduled:
        return True
    return False

def should_skip_chapter_in_epub(chapter_metadata, include_drafts=False):
    """Check if a chapter should be skipped in EPUB generation"""
    if chapter_metadata is None:
        return True
    if chapter_metadata.get('hidden', False):
        return True
    if chapter_metadata.get('draft', False) and not include_drafts:
        return True
    if chapter_metadata.get('scheduled', False):
        return True  # Always skip scheduled chapters in EPUB
    if 'password' in chapter_metadata and chapter_metadata['password']:
        return True  # Skip password-protected chapters in EPUB
    return False

def is_chapter_hidden(chapter_metadata):
    """Check if a chapter is marked as hidden"""
    if chapter_metadata is None:
        return False
    return chapter_metadata.get('hidden', False)

def parse_publish_date(date_string):
    """Parse publication date from various formats"""
    if not date_string:
        return None
    
    # Try different date formats
    formats = [
        '%Y-%m-%d',
        '%Y-%m-%d %H:%M:%S',
        '%Y-%m-%dT%H:%M:%S',
        '%Y-%m-%dT%H:%M:%S%z',
        '%Y-%m-%dT%H:%M:%S.%f',
        '%Y-%m-%dT%H:%M:%S.%f%z',
        '%a, %d %b %Y %H:%M:%S %z',  # RFC 2822 format
        '%a, %d %b %Y %H:%M:%S %Z',
        '%B %d, %Y',  # "January 15, 2023"
        '%b %d, %Y',  # "Jan 15, 2023"
        '%d %B %Y',   # "15 January 2023"
        '%d %b %Y',   # "15 Jan 2023"
        '%m/%d/%Y',   # "01/15/2023"
        '%d/%m/%Y',   # "15/01/2023"
    ]
    
    for fmt in formats:
        try:
            return datetime.datetime.strptime(date_string, fmt)
        except ValueError:
            continue
    
    # If no format matches, return None
    return None

def format_date_for_display(date_string):
    """Format date for display in templates"""
    parsed_date = parse_publish_date(date_string)
    if parsed_date:
        return parsed_date.strftime('%B %d, %Y')
    return date_string

def find_author_username_filter(author_name):
    """Jinja2 filter to find author username"""
    authors_config = load_authors_config()
    return find_author_username(author_name, authors_config)

def slugify_tag(tag):
    """Convert tag to URL-safe slug"""
    return re.sub(r'[^a-zA-Z0-9]+', '-', tag.lower()).strip('-')

# Register additional filters
env.filters['slugify_tag'] = slugify_tag
env.filters['format_date_for_display'] = format_date_for_display
env.filters['find_author_username'] = find_author_username_filter

def collect_tags_for_novel(novel_slug, language='en'):
    """Collect all tags used in a novel's chapters"""
    tags = {}
    
    novel_config = load_novel_config(novel_slug)
    
    # Get primary language if not specified
    if language == 'en' and novel_config.get('primary_language'):
        language = novel_config['primary_language']
    
    for arc in novel_config.get('arcs', []):
        for chapter in arc.get('chapters', []):
            chapter_id = chapter['id']
            
            # Load chapter content
            try:
                chapter_content, chapter_metadata = load_chapter_content(novel_slug, chapter_id, language)
                
                # Skip hidden/draft chapters
                if should_skip_chapter(chapter_metadata, INCLUDE_DRAFTS, INCLUDE_SCHEDULED):
                    continue
                
                chapter_tags = chapter_metadata.get('tags', [])
                if isinstance(chapter_tags, str):
                    chapter_tags = [chapter_tags]
                
                for tag in chapter_tags:
                    tag_slug = slugify_tag(tag)
                    if tag_slug not in tags:
                        tags[tag_slug] = {
                            'name': tag,
                            'slug': tag_slug,
                            'count': 0,
                            'chapters': []
                        }
                    tags[tag_slug]['count'] += 1
                    tags[tag_slug]['chapters'].append({
                        'id': chapter_id,
                        'title': chapter['title']
                    })
            except:
                continue
    
    return tags

def collect_all_tags(site_config, novels_data):
    """Collect tags from all novels for global tag pages"""
    all_tags = {}
    
    for novel in novels_data:
        novel_slug = novel['slug']
        novel_config = load_novel_config(novel_slug)
        novel_title = novel_config.get('title', novel_slug)
        
        # Skip novels that don't allow indexing
        if novel_config.get('seo', {}).get('allow_indexing') is False:
            continue
        
        available_languages = get_available_languages(novel_slug)
        
        for language in available_languages:
            novel_tags = collect_tags_for_novel(novel_slug, language)
            
            for tag_slug, tag_data in novel_tags.items():
                if tag_slug not in all_tags:
                    all_tags[tag_slug] = {
                        'name': tag_data['name'],
                        'slug': tag_slug,
                        'novels': []
                    }
                
                # Add this novel to the tag if not already present
                novel_in_tag = next((n for n in all_tags[tag_slug]['novels'] if n['slug'] == novel_slug), None)
                if not novel_in_tag:
                    all_tags[tag_slug]['novels'].append({
                        'slug': novel_slug,
                        'title': novel_title,
                        'count': tag_data['count']
                    })
                else:
                    novel_in_tag['count'] += tag_data['count']
    
    return all_tags

def is_chapter_new(chapter_metadata, new_chapter_threshold_days=7):
    """Check if a chapter is considered "new" based on publish date"""
    if not chapter_metadata or 'published' not in chapter_metadata:
        return False
    
    published_date = parse_publish_date(chapter_metadata['published'])
    if not published_date:
        return False
    
    now = datetime.datetime.now()
    days_since_publish = (now - published_date).days
    
    return days_since_publish <= new_chapter_threshold_days

def get_novels_data():
    """Get list of all novels with their metadata"""
    novels = []
    
    if not os.path.exists(CONTENT_DIR):
        return novels
    
    for item in os.listdir(CONTENT_DIR):
        item_path = os.path.join(CONTENT_DIR, item)
        if os.path.isdir(item_path) and not item.startswith('.'):
            config = load_novel_config(item)
            if config:
                novel_data = {
                    'slug': item,
                    'title': config.get('title', item),
                    'description': config.get('description', ''),
                    'primary_language': config.get('primary_language', 'en'),
                    'status': config.get('status', 'ongoing'),
                    'tags': config.get('tags', []),
                    'arcs': []
                }
                
                # Process arcs and chapters
                for arc in config.get('arcs', []):
                    arc_data = {
                        'title': arc['title'],
                        'chapters': arc['chapters']
                    }
                    novel_data['arcs'].append(arc_data)
                
                novels.append(novel_data)
    
    return novels

def generate_epub_for_novel(novel_slug, novel_config, language='en', include_drafts=False):
    """Generate EPUB file for a novel"""
    if not _check_ebooklib():
        print(f"Warning: Skipping EPUB generation for {novel_slug} - ebooklib not available")
        return
    
    try:
        from ebooklib import epub
        
        # Get chapters for EPUB
        chapters_data = get_chapters_for_epub(novel_config, novel_slug, language, include_drafts)
        
        if not chapters_data:
            print(f"Warning: No chapters found for EPUB generation of {novel_slug}")
            return
        
        # Create EPUB book
        book = epub.EpubBook()
        
        # Set metadata
        book.set_identifier(f"{novel_slug}-{language}")
        book.set_title(novel_config.get('title', novel_slug))
        book.set_language(language)
        
        if novel_config.get('author', {}).get('name'):
            book.add_author(novel_config['author']['name'])
        
        # Add chapters
        epub_chapters = []
        toc_items = []
        
        for arc_idx, arc in enumerate(chapters_data):
            arc_title = arc['title']
            
            for chapter_idx, chapter in enumerate(arc['chapters']):
                chapter_id = chapter['id']
                chapter_title = chapter['title']
                
                # Create chapter content
                html_content = convert_markdown_to_html(chapter['content'])
                
                # Create EPUB chapter
                epub_chapter = epub.EpubHtml(
                    title=chapter_title,
                    file_name=f'chapter_{chapter_id}.xhtml',
                    lang=language
                )
                
                # Add content with proper HTML structure
                epub_chapter.content = f"""<html xmlns="http://www.w3.org/1999/xhtml">
<head>
<title>{chapter_title}</title>
</head>
<body>
<h1>{chapter_title}</h1>
{html_content}
</body>
</html>"""
                
                book.add_item(epub_chapter)
                epub_chapters.append(epub_chapter)
                
                # Add to TOC
                toc_items.append(epub_chapter)
        
        # Add navigation files
        book.add_item(epub.EpubNcx())
        book.add_item(epub.EpubNav())
        
        # Define Table of Contents
        book.toc = toc_items
        
        # Add default NCX and Nav file
        book.add_item(epub.EpubNcx())
        book.add_item(epub.EpubNav())
        
        # Define CSS style
        style = '''
body { font-family: Arial, sans-serif; margin: 20px; }
h1 { color: #333; }
'''
        nav_css = epub.EpubItem(uid="style_nav", file_name="style/nav.css", media_type="text/css", content=style)
        book.add_item(nav_css)
        
        # Create spine
        book.spine = ['nav'] + epub_chapters
        
        # Save EPUB file
        epub_filename = f"{novel_slug}_{language}.epub"
        epub_path = os.path.join(BUILD_DIR, epub_filename)
        
        epub.write_epub(epub_path, book, {})
        
        print(f"Generated EPUB: {epub_path}")
        
        return epub_filename
        
    except Exception as e:
        print(f"Error generating EPUB for {novel_slug}: {e}")
        return None

def copy_static_files():
    """Copy static files to build directory"""
    if not os.path.exists(STATIC_DIR):
        return
    
    # Ensure build static directory exists
    build_static_dir = os.path.join(BUILD_DIR, "static")
    os.makedirs(build_static_dir, exist_ok=True)
    
    # Copy all files from static directory
    for root, dirs, files in os.walk(STATIC_DIR):
        # Calculate relative path
        rel_path = os.path.relpath(root, STATIC_DIR)
        build_path = os.path.join(build_static_dir, rel_path)
        
        # Create directories
        for dir_name in dirs:
            os.makedirs(os.path.join(build_path, dir_name), exist_ok=True)
        
        # Copy files with minification if enabled
        for file_name in files:
            source_file = os.path.join(root, file_name)
            dest_file = os.path.join(build_path, file_name)
            
            # Minify CSS and JS files if minification is enabled
            if should_minify() and file_name.endswith('.css'):
                try:
                    with open(source_file, 'r', encoding='utf-8') as f:
                        css_content = f.read()
                    minified_css = minify_css_content(css_content)
                    with open(dest_file, 'w', encoding='utf-8') as f:
                        f.write(minified_css)
                except Exception as e:
                    # If minification fails, copy original
                    shutil.copy2(source_file, dest_file)
                    print(f"Warning: CSS minification failed for {file_name}: {e}")
            elif should_minify() and file_name.endswith('.js'):
                try:
                    with open(source_file, 'r', encoding='utf-8') as f:
                        js_content = f.read()
                    minified_js = minify_js_content(js_content)
                    with open(dest_file, 'w', encoding='utf-8') as f:
                        f.write(minified_js)
                except Exception as e:
                    # If minification fails, copy original
                    shutil.copy2(source_file, dest_file)
                    print(f"Warning: JS minification failed for {file_name}: {e}")
            else:
                shutil.copy2(source_file, dest_file)
            
            # Build asset map for cache busting
            rel_source_path = os.path.relpath(source_file, STATIC_DIR)
            ASSET_MAP[rel_source_path] = rel_source_path

def generate_site():
    """Main function to generate the entire site"""
    print("Starting site generation...")
    
    # Load site configuration
    site_config = load_site_config()
    if not site_config:
        print("Error: site_config.yaml not found or invalid")
        return
    
    # Get novels data
    novels_data = get_novels_data()
    
    # Copy static files
    copy_static_files()
    
    # Generate RSS feeds
    print("Generating RSS feeds...")
    site_rss = generate_rss_feed(site_config, novels_data)
    with open(os.path.join(BUILD_DIR, "rss.xml"), "w", encoding='utf-8') as f:
        f.write(site_rss)
    
    # Generate sitemap
    print("Generating sitemap...")
    sitemap = generate_sitemap_xml(site_config, novels_data)
    with open(os.path.join(BUILD_DIR, "sitemap.xml"), "w", encoding='utf-8') as f:
        f.write(sitemap)
    
    # Generate robots.txt
    print("Generating robots.txt...")
    robots = generate_robots_txt(site_config, novels_data)
    with open(os.path.join(BUILD_DIR, "robots.txt"), "w", encoding='utf-8') as f:
        f.write(robots)
    
    # Generate front page
    print("Generating front page...")
    generate_front_page(site_config, novels_data)
    
    # Generate pages
    print("Generating pages...")
    generate_pages(site_config, novels_data)
    
    # Generate novel pages
    print("Generating novel pages...")
    for novel in novels_data:
        generate_novel_pages(site_config, novel)
    
    # Generate tag pages
    print("Generating tag pages...")
    generate_tag_pages(site_config, novels_data)
    
    # Generate author pages
    print("Generating author pages...")
    generate_author_pages(site_config, novels_data)
    
    print("Site generation complete!")

def generate_front_page(site_config, novels_data):
    """Generate the front page"""
    # Load webring configuration
    webring_config = load_webring_config()
    webring_data = generate_webring_data(webring_config, webring_config.get('display', {}))
    
    # Build SEO and social meta for front page
    seo_meta = build_seo_meta(site_config, None, None, 'site')
    social_meta = build_social_meta(site_config, None, None, 'site', site_config.get('site_name', ''), site_config.get('site_url', '').rstrip('/') + '/')
    
    # Build navigation for front page
    navigation = build_page_navigation(site_config, 'en')
    
    template = env.get_template("index.html")
    html_content = template.render(
        site_config=site_config,
        novels=novels_data,
        webring=webring_data,
        webring_config=webring_config,
        seo_meta=seo_meta,
        social_meta=social_meta,
        navigation=navigation,
        footer=build_footer_content(site_config)
    )
    write_html_file(os.path.join(BUILD_DIR, "index.html"), html_content, should_minify())

def generate_pages(site_config, novels_data):
    """Generate static pages"""
    all_pages = get_all_pages()
    
    for page_data in all_pages:
        page_slug = page_data['slug']
        page_title = page_data['title']
        
        print(f"  Generating page: {page_slug}")
        
        available_languages = get_available_page_languages(page_slug)
        
        for language in available_languages:
            try:
                content_md, metadata = load_page_content(page_slug, language)
                
                if should_skip_page(metadata, INCLUDE_DRAFTS):
                    continue
                
                # Convert markdown to HTML
                content_html = convert_markdown_to_html(content_md)
                
                # Build page URL
                if '/' in page_slug:
                    page_dir = os.path.join(BUILD_DIR, page_slug, language)
                    page_file = "index.html"
                else:
                    page_dir = os.path.join(BUILD_DIR, page_slug, language)
                    page_file = "index.html"
                
                os.makedirs(page_dir, exist_ok=True)
                
                # Build navigation
                navigation = build_page_navigation(site_config, language, page_slug)
                
                # Build SEO meta
                seo_meta = build_seo_meta(site_config, None, metadata, 'page')
                
                # Build social meta
                page_url = f"{site_config.get('site_url', '').rstrip('/')}/{page_slug}/{language}/"
                social_meta = build_social_meta(site_config, None, metadata, 'page', page_title, page_url)
                
                template = env.get_template("page.html")
                html_content = template.render(
                    site_config=site_config,
                    page_title=page_title,
                    page_content=content_html,
                    page_metadata=metadata,
                    navigation=navigation,
                    seo_meta=seo_meta,
                    social_meta=social_meta,
                    footer=build_footer_content(site_config, None, 'page'),
                    current_page=page_slug,
                    current_language=language
                )
                write_html_file(os.path.join(page_dir, page_file), html_content, should_minify())
                
            except Exception as e:
                print(f"    Error generating page {page_slug} ({language}): {e}")

def generate_novel_pages(site_config, novel):
    """Generate pages for a specific novel"""
    novel_slug = novel['slug']
    novel_config = load_novel_config(novel_slug)
    
    # Skip novels that don't allow indexing
    if novel_config.get('seo', {}).get('allow_indexing') is False:
        return
    
    available_languages = get_available_languages(novel_slug)
    
    print(f"  Generating novel: {novel_slug}")
    
    # Generate TOC pages
    for language in available_languages:
        generate_toc_page(site_config, novel_slug, novel_config, language)
    
    # Generate chapter pages
    for language in available_languages:
        generate_chapter_pages(site_config, novel_slug, novel_config, language)
    
    # Generate EPUB if enabled
    if novel_config.get('downloads', {}).get('epub_enabled', False):
        primary_lang = novel_config.get('primary_language', 'en')
        epub_filename = generate_epub_for_novel(novel_slug, novel_config, primary_lang, INCLUDE_DRAFTS)
        if epub_filename:
            novel_config['epub_filename'] = epub_filename
    
    # Generate tag pages for this novel
    for language in available_languages:
        generate_novel_tag_pages(site_config, novel_slug, novel_config, language)
    
    # Generate story-specific RSS feed
    story_rss = generate_rss_feed(site_config, None, novel_config, novel_slug)
    rss_path = os.path.join(BUILD_DIR, novel_slug, "rss.xml")
    os.makedirs(os.path.dirname(rss_path), exist_ok=True)
    with open(rss_path, "w", encoding='utf-8') as f:
        f.write(story_rss)

def generate_toc_page(site_config, novel_slug, novel_config, language='en'):
    """Generate table of contents page for a novel"""
    # Get non-hidden chapters
    chapters_data = get_non_hidden_chapters(novel_config, novel_slug, language, INCLUDE_DRAFTS, INCLUDE_SCHEDULED)
    
    # Build navigation
    navigation = build_page_navigation(site_config, language)
    
    # Build SEO meta
    seo_meta = build_seo_meta(site_config, novel_config, None, 'toc')
    
    # Build social meta
    toc_title = f"{novel_config.get('title', novel_slug)} - Table of Contents"
    toc_url = f"{site_config.get('site_url', '').rstrip('/')}/{novel_slug}/{language}/toc/"
    social_meta = build_social_meta(site_config, novel_config, None, 'toc', toc_title, toc_url)
    
    # Get new chapter threshold
    new_chapter_threshold = site_config.get('new_chapter_tags', {}).get('threshold_days', 7)
    
    # Create is_chapter_new filter with proper config
    def is_chapter_new_filter(chapter_metadata):
        return is_chapter_new(chapter_metadata, new_chapter_threshold)
    
    novel_env = get_novel_template_env(novel_slug)
    novel_env.filters['is_chapter_new'] = is_chapter_new_filter
    
    template = novel_env.get_template("toc.html")
    html_content = template.render(
        site_config=site_config,
        novel_config=novel_config,
        chapters=chapters_data,
        navigation=navigation,
        seo_meta=seo_meta,
        social_meta=social_meta,
        footer=build_footer_content(site_config, novel_config, 'toc'),
        current_novel=novel_slug,
        current_language=language,
        novel_slug=novel_slug
    )
    
    toc_dir = os.path.join(BUILD_DIR, novel_slug, language, "toc")
    os.makedirs(toc_dir, exist_ok=True)
    write_html_file(os.path.join(toc_dir, "index.html"), html_content, should_minify())

def generate_chapter_pages(site_config, novel_slug, novel_config, language='en'):
    """Generate individual chapter pages for a novel"""
    # Process cover art
    processed_images = process_cover_art(novel_slug, novel_config)
    
    for arc in novel_config.get('arcs', []):
        for chapter in arc.get('chapters', []):
            chapter_id = chapter['id']
            
            try:
                content_md, metadata = load_chapter_content(novel_slug, chapter_id, language)
                
                if content_md is None:
                    continue
                
                if should_skip_chapter(metadata, INCLUDE_DRAFTS, INCLUDE_SCHEDULED):
                    continue
                
                # Convert markdown to HTML
                content_html = convert_markdown_to_html(content_md)
                
                # Build navigation
                navigation = build_page_navigation(site_config, language)
                
                # Build SEO meta
                seo_meta = build_seo_meta(site_config, novel_config, metadata, 'chapter')
                
                # Build social meta
                chapter_title = metadata.get('title', chapter['title'])
                chapter_url = f"{site_config.get('site_url', '').rstrip('/')}/{novel_slug}/{language}/{chapter_id}/"
                social_meta = build_social_meta(site_config, novel_config, metadata, 'chapter', chapter_title, chapter_url)
                
                # Get new chapter threshold
                new_chapter_threshold = site_config.get('new_chapter_tags', {}).get('threshold_days', 7)
                
                # Create is_chapter_new filter with proper config
                def is_chapter_new_filter(chapter_metadata):
                    return is_chapter_new(chapter_metadata, new_chapter_threshold)
                
                novel_env = get_novel_template_env(novel_slug)
                novel_env.filters['is_chapter_new'] = is_chapter_new_filter
                
                template = novel_env.get_template("chapter.html")
                html_content = template.render(
                    site_config=site_config,
                    novel_config=novel_config,
                    chapter_content=content_html,
                    chapter_metadata=metadata,
                    chapter_id=chapter_id,
                    navigation=navigation,
                    seo_meta=seo_meta,
                    social_meta=social_meta,
                    footer=build_footer_content(site_config, novel_config, 'chapter'),
                    current_novel=novel_slug,
                    current_language=language,
                    processed_images=processed_images
                )
                
                chapter_dir = os.path.join(BUILD_DIR, novel_slug, language, chapter_id)
                os.makedirs(chapter_dir, exist_ok=True)
                write_html_file(os.path.join(chapter_dir, "index.html"), html_content, should_minify())
                
            except Exception as e:
                print(f"    Error generating chapter {chapter_id}: {e}")

def generate_novel_tag_pages(site_config, novel_slug, novel_config, language='en'):
    """Generate tag pages for a specific novel"""
    tags_data = collect_tags_for_novel(novel_slug, language)
    
    for tag_slug, tag_info in tags_data.items():
        # Build navigation
        navigation = build_page_navigation(site_config, language)
        
        # Build SEO meta
        seo_meta = build_seo_meta(site_config, novel_config, None, 'tag')
        
        # Build social meta
        tag_title = f"{novel_config.get('title', novel_slug)} - {tag_info['name']} Chapters"
        tag_url = f"{site_config.get('site_url', '').rstrip('/')}/{novel_slug}/{language}/tags/{tag_slug}/"
        social_meta = build_social_meta(site_config, novel_config, None, 'tag', tag_title, tag_url)
        
        # Get new chapter threshold
        new_chapter_threshold = site_config.get('new_chapter_tags', {}).get('threshold_days', 7)
        
        # Create is_chapter_new filter with proper config
        def is_chapter_new_filter(chapter_metadata):
            return is_chapter_new(chapter_metadata, new_chapter_threshold)
        
        novel_env = get_novel_template_env(novel_slug)
        novel_env.filters['is_chapter_new'] = is_chapter_new_filter
        
        template = novel_env.get_template("tag_page.html")
        html_content = template.render(
            site_config=site_config,
            novel_config=novel_config,
            tag_info=tag_info,
            navigation=navigation,
            seo_meta=seo_meta,
            social_meta=social_meta,
            footer=build_footer_content(site_config, novel_config, 'tag'),
            current_novel=novel_slug,
            current_language=language
        )
        
        tag_dir = os.path.join(BUILD_DIR, novel_slug, language, "tags", tag_slug)
        os.makedirs(tag_dir, exist_ok=True)
        write_html_file(os.path.join(tag_dir, "index.html"), html_content, should_minify())
    
    # Generate novel tag index page
    if tags_data:
        # Build navigation
        navigation = build_page_navigation(site_config, language)
        
        # Build SEO meta
        seo_meta = build_seo_meta(site_config, novel_config, None, 'tags')
        
        # Build social meta
        tags_title = f"{novel_config.get('title', novel_slug)} - Tags"
        tags_url = f"{site_config.get('site_url', '').rstrip('/')}/{novel_slug}/{language}/tags/"
        social_meta = build_social_meta(site_config, novel_config, None, 'tags', tags_title, tags_url)
        
        template = novel_env.get_template("tags_index.html")
        html_content = template.render(
            site_config=site_config,
            novel_config=novel_config,
            tags=tags_data,
            navigation=navigation,
            seo_meta=seo_meta,
            social_meta=social_meta,
            footer=build_footer_content(site_config, novel_config, 'tags'),
            current_novel=novel_slug,
            current_language=language
        )
        
        tags_index_dir = os.path.join(BUILD_DIR, novel_slug, language, "tags")
        os.makedirs(tags_index_dir, exist_ok=True)
        write_html_file(os.path.join(tags_index_dir, "index.html"), html_content, should_minify())

def generate_tag_pages(site_config, novels_data):
    """Generate global tag pages"""
    all_tags = collect_all_tags(site_config, novels_data)
    
    for tag_slug, tag_info in all_tags.items():
        # Build navigation
        navigation = build_page_navigation(site_config, 'en')  # Global tags use default language
        
        # Build SEO meta
        seo_meta = build_seo_meta(site_config, None, None, 'global_tag')
        
        # Build social meta
        global_tag_title = f"All {tag_info['name']} Chapters"
        global_tag_url = f"{site_config.get('site_url', '').rstrip('/')}/tags/{tag_slug}/"
        social_meta = build_social_meta(site_config, None, None, 'global_tag', global_tag_title, global_tag_url)
        
        template = env.get_template("global_tag_page.html")
        html_content = template.render(
            site_config=site_config,
            tag_info=tag_info,
            navigation=navigation,
            seo_meta=seo_meta,
            social_meta=social_meta,
            footer=build_footer_content(site_config, None, 'global_tag'),
            current_language='en'
        )
        
        tag_dir = os.path.join(BUILD_DIR, "tags", tag_slug)
        os.makedirs(tag_dir, exist_ok=True)
        write_html_file(os.path.join(tag_dir, "index.html"), html_content, should_minify())
    
    # Generate global tags index page
    if all_tags:
        # Build navigation
        navigation = build_page_navigation(site_config, 'en')
        
        # Build SEO meta
        seo_meta = build_seo_meta(site_config, None, None, 'global_tags')
        
        # Build social meta
        global_tags_title = "All Tags"
        global_tags_url = f"{site_config.get('site_url', '').rstrip('/')}/tags/"
        social_meta = build_social_meta(site_config, None, None, 'global_tags', global_tags_title, global_tags_url)
        
        template = env.get_template("global_tags_index.html")
        html_content = template.render(
            site_config=site_config,
            tags=all_tags,
            navigation=navigation,
            seo_meta=seo_meta,
            social_meta=social_meta,
            footer=build_footer_content(site_config, None, 'global_tags'),
            current_language='en'
        )
        
        tags_index_dir = os.path.join(BUILD_DIR, "tags")
        os.makedirs(tags_index_dir, exist_ok=True)
        write_html_file(os.path.join(tags_index_dir, "index.html"), html_content, should_minify())

def generate_author_pages(site_config, novels_data):
    """Generate author pages"""
    author_contributions = collect_author_contributions(novels_data)
    authors_config = load_authors_config()
    
    for author_name, contributions in author_contributions.items():
        # Get username for author
        username = find_author_username(author_name, authors_config)
        if username is None:
            continue  # Skip authors not in config
        
        # Get author info from config
        author_info = authors_config.get(username, {})
        
        # Build navigation
        navigation = build_page_navigation(site_config, 'en')
        
        # Build SEO meta
        seo_meta = build_seo_meta(site_config, None, None, 'author')
        
        # Build social meta
        author_title = f"{author_name} - Author"
        author_url = f"{site_config.get('site_url', '').rstrip('/')}/authors/{username}/"
        social_meta = build_social_meta(site_config, None, None, 'author', author_title, author_url)
        
        template = env.get_template("author.html")
        html_content = template.render(
            site_config=site_config,
            author_name=author_name,
            author_info=author_info,
            contributions=contributions,
            navigation=navigation,
            seo_meta=seo_meta,
            social_meta=social_meta,
            footer=build_footer_content(site_config, None, 'author'),
            current_language='en'
        )
        
        author_dir = os.path.join(BUILD_DIR, "authors", username)
        os.makedirs(author_dir, exist_ok=True)
        write_html_file(os.path.join(author_dir, "index.html"), html_content, should_minify())
    
    # Generate authors index page
    if author_contributions:
        # Build navigation
        navigation = build_page_navigation(site_config, 'en')
        
        # Build SEO meta
        seo_meta = build_seo_meta(site_config, None, None, 'authors')
        
        # Build social meta
        authors_title = "Authors"
        authors_url = f"{site_config.get('site_url', '').rstrip('/')}/authors/"
        social_meta = build_social_meta(site_config, None, None, 'authors', authors_title, authors_url)
        
        author_usernames = {name: find_author_username(name, authors_config) for name in author_contributions}
        
        template = env.get_template("authors.html")
        html_content = template.render(
            site_config=site_config,
            authors=author_contributions,
            authors_config=authors_config,
            author_usernames=author_usernames,
            navigation=navigation,
            seo_meta=seo_meta,
            social_meta=social_meta,
            footer=build_footer_content(site_config, None, 'authors'),
            current_language='en'
        )
        
        authors_index_dir = os.path.join(BUILD_DIR, "authors")
        os.makedirs(authors_index_dir, exist_ok=True)
        write_html_file(os.path.join(authors_index_dir, "index.html"), html_content, should_minify())

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Generate static web novel site')
    parser.add_argument('--include-drafts', action='store_true', help='Include draft chapters in generation')
    parser.add_argument('--include-scheduled', action='store_true', help='Include scheduled chapters in generation')
    parser.add_argument('--serve', action='store_true', help='Start development server instead of generating')
    parser.add_argument('--port', type=int, default=8000, help='Port for development server')
    parser.add_argument('--no-minify', action='store_true', help='Disable HTML/CSS/JS minification')
    
    args = parser.parse_args()
    
    INCLUDE_DRAFTS = args.include_drafts
    INCLUDE_SCHEDULED = args.include_scheduled
    
    if args.serve:
        print("Starting development server...")
        # Import here to avoid requiring it for static generation
        try:
            from http.server import HTTPServer, SimpleHTTPRequestHandler
            import threading
            import time
            from watchdog.observers import Observer
            from watchdog.events import FileSystemEventHandler
            
            class SiteChangeHandler(FileSystemEventHandler):
                def __init__(self):
                    self.last_build = 0
                    
                def on_modified(self, event):
                    if event.is_directory:
                        return
                    
                    # Skip certain file types and paths
                    if (event.src_path.endswith(('.pyc', '__pycache__')) or
                        '/.git/' in event.src_path or
                        '/build/' in event.src_path):
                        return
                    
                    current_time = time.time()
                    if current_time - self.last_build > 1:  # Debounce rebuilds
                        print(f"File changed: {event.src_path}")
                        try:
                            generate_site()
                            print("Site rebuilt successfully")
                        except Exception as e:
                            print(f"Error rebuilding site: {e}")
                        self.last_build = current_time
            
            # Initial build
            generate_site()
            
            # Start file watcher
            observer = Observer()
            observer.schedule(SiteChangeHandler(), '.', recursive=True)
            observer.start()
            
            # Start HTTP server
            server = HTTPServer(('localhost', args.port), SimpleHTTPRequestHandler)
            print(f"Server running at http://localhost:{args.port}/build/")
            print("Press Ctrl+C to stop")
            
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                print("Stopping server...")
                observer.stop()
                observer.join()
                
        except ImportError as e:
            print(f"Error: Required dependencies for serve mode not installed: {e}")
            print("Install with: pip install watchdog")
    else:
        generate_site()