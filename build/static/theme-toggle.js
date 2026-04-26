(function(){'use strict';function getTheme(){return localStorage.getItem('theme')||'light';}
function setTheme(theme){localStorage.setItem('theme',theme);document.documentElement.setAttribute('data-theme',theme);}
function toggleTheme(){const currentTheme=getTheme();const newTheme=currentTheme==='light'?'dark':'light';setTheme(newTheme);}
function initTheme(){const theme=getTheme();setTheme(theme);}
function createThemeToggle(){const button=document.createElement('button');button.id='theme-toggle';button.innerHTML='🌓';button.title='Toggle theme';button.style.cssText=`
            position: fixed;
            bottom: 20px;
            right: 20px;
            width: 50px;
            height: 50px;
            border-radius: 50%;
            border: none;
            background: var(--primary-color);
            color: white;
            font-size: 20px;
            cursor: pointer;
            box-shadow: var(--box-shadow);
            z-index: 1000;
            transition: all 0.2s;
        `;button.addEventListener('click',toggleTheme);button.addEventListener('mouseenter',()=>{button.style.transform='scale(1.1)';});button.addEventListener('mouseleave',()=>{button.style.transform='scale(1)';});document.body.appendChild(button);}
if(document.readyState==='loading'){document.addEventListener('DOMContentLoaded',function(){initTheme();createThemeToggle();});}else{initTheme();createThemeToggle();}
window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change',function(e){if(!localStorage.getItem('theme')){const newTheme=e.matches?'dark':'light';setTheme(newTheme);}});})();